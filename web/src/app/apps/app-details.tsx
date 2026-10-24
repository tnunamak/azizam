"use client";

import { useQuery } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ListRow } from "@/components/product/list-card";
import { RelativeTime } from "@/components/product/relative-time";
import type { AccountApp } from "@/lib/account-api";
import type { Credential } from "@/lib/api";
import { keyStart } from "./rename";
import { listTrustedAddresses, trustedAddressesKey } from "./trusted-addresses";

/*
 * What an app owns, shown as rows under its row on the Apps page: its keys,
 * and for an owner its trusted addresses. They stay in the same list as the
 * apps, tinted and set in under the app's name, so the page keeps one app list.
 */

const childRowClass = "bg-muted/30";

/** The app's keys, one row each: "Key · name", the masked start, and when it was last used. */
export function AppKeyRows({
  app,
  busy,
  onRotate,
  onRevoke,
}: {
  app: AccountApp;
  busy: boolean;
  onRotate: (key: Credential) => void;
  onRevoke: (key: Credential) => void;
}) {
  return app.credentials.map((key) => (
    <ListRow
      key={key.id}
      lines={2}
      titleAs="h3"
      className={childRowClass}
      leading={<KeyRound aria-hidden="true" />}
      title={`Key · ${key.display_name}`}
      meta={[
        <span key="start" className="font-mono">
          {keyStart(key.prefix)}
        </span>,
        key.last_used_at != null ? (
          <>
            Last used <RelativeTime ts={key.last_used_at} />
          </>
        ) : (
          "Never used"
        ),
      ]}
      trailing={
        <>
          <Button
            variant="outline"
            size="sm"
            disabled={busy || !app.can_mint_keys}
            aria-label={`Rotate key ${key.display_name}`}
            onClick={() => onRotate(key)}
          >
            Rotate
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={busy}
            aria-label={`Revoke key ${key.display_name}`}
            onClick={() => onRevoke(key)}
          >
            Revoke
          </Button>
        </>
      }
    />
  ));
}

/**
 * An owner's view of the app's trusted addresses. A request from one of them,
 * with no key, uses this app's access plan. The dialog behind Manage makes changes.
 */
export function AppTrustedRows({
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
  // A spacer as wide as a row icon, so these lines start under the app's name.
  const indent = <span aria-hidden="true" className="block size-4 shrink-0" />;
  return (
    <>
      <li className={`${childRowClass} flex items-start gap-3 px-4 py-3`}>
        {indent}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-body font-medium">Trusted addresses (keyless access)</h3>
            <Button
              variant="outline"
              size="sm"
              aria-label={`Manage trusted addresses for ${appName}`}
              onClick={onManage}
            >
              Manage
            </Button>
          </div>
          <p className="text-muted-foreground text-meta">
            Requests from these addresses use this app&apos;s access plan when
            they omit a key.
          </p>
        </div>
      </li>
      {isPending ? (
        <li className={`${childRowClass} flex items-center gap-3 px-4 py-2`}>
          {indent}
          <Skeleton className="h-6 w-full" />
        </li>
      ) : isError ? (
        <li className={`${childRowClass} flex items-center gap-3 px-4 py-2`}>
          {indent}
          <p role="alert" className="text-destructive text-meta">
            The trusted addresses could not be loaded.
          </p>
        </li>
      ) : data.length ? (
        data.map((entry) => (
          <li
            key={entry.id}
            className={`${childRowClass} flex items-baseline gap-3 px-4 py-2`}
          >
            {indent}
            <p className="flex min-w-0 flex-wrap items-baseline gap-x-3">
              <span className="font-mono text-sm break-all">{entry.address}</span>
              {entry.note ? (
                <span className="text-muted-foreground text-meta">{entry.note}</span>
              ) : null}
              {entry.cutover ? (
                <span className="text-warning text-meta">Needs a quick check</span>
              ) : null}
            </p>
          </li>
        ))
      ) : (
        <li className={`${childRowClass} flex items-center gap-3 px-4 py-2`}>
          {indent}
          <p className="text-muted-foreground text-meta">
            None yet. Requests without a key are refused.
          </p>
        </li>
      )}
    </>
  );
}
