"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { adminFetch } from "@/lib/api";
import { accountApi } from "@/lib/account-api";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";

export interface TrustedAddress {
  id: string;
  app_id: string;
  address: string;
  note: string | null;
  created_at: number;
  /** Null when the address was made before its creator was recorded. */
  created_by: string | null;
  /** Present only when the cut-over would remove the address: keep it, or add it again. */
  cutover?: "keep" | "readd";
  /** The person recorded as its maker, when that is another person. */
  made_by_name?: string;
}

const listPath = (appId: string) =>
  `/admin/api/apps/${encodeURIComponent(appId)}/trusted-addresses`;

/** What to show for a failed change: the gateway's words, or the fallback. */
function messageOf(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message ? caught.message : fallback;
}

/** An owner trusts a device, or a range of them, to use one app with no
 * key. The app's access plan still limits what it can do. */
export function TrustedAddressesDialog({
  app,
  appName,
  onClose,
  onChanged,
}: {
  app: { id: string } | null;
  appName: string;
  onClose: () => void;
  /** An address was kept or added again: the account's Apps list shows the new count. */
  onChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const [address, setAddress] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [keptId, setKeptId] = useState<string | null>(null);
  const appId = app?.id ?? "";
  const key = ["admin", "app-trusted-addresses", appId];
  const { data, isPending, isError } = useQuery({
    queryKey: key,
    enabled: app !== null,
    queryFn: () =>
      adminFetch<{ trusted_addresses: TrustedAddress[] }>(listPath(appId)).then(
        (r) => r.trusted_addresses,
      ),
  });

  async function change(run: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await run();
      await queryClient.invalidateQueries({ queryKey: key });
      return true;
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "The change was not saved.",
      );
      return false;
    } finally {
      setBusy(false);
    }
  }

  // After a keep the entry's Keep button is gone, so focus its Remove button once the change has settled.
  useEffect(() => {
    if (keptId !== null && !busy) {
      document.getElementById(`remove-address-${keptId}`)?.focus();
      setKeptId(null);
    }
  }, [keptId, busy]);

  async function add(event: FormEvent) {
    event.preventDefault();
    const saved = await change(() =>
      adminFetch(listPath(appId), {
        method: "POST",
        body: { address, note },
      }),
    );
    if (saved) {
      setAddress("");
      setNote("");
    }
  }

  async function keep(entry: TrustedAddress) {
    const kept = await change(() => accountApi.keepTrustedAddress(appId, entry.id));
    if (kept) {
      setKeptId(entry.id);
      onChanged();
      toast.success("Address kept");
    }
  }

  // An App holds each address once, so the old entry is removed before the same address is added again. The add
  // then records this person as the address's maker. Focus moves to the new entry's Remove button.
  async function addAgain(entry: TrustedAddress) {
    setBusy(true);
    setError(null);
    let addedId: string | null = null;
    try {
      await accountApi.removeTrustedAddress(appId, entry.id);
      try {
        const added = await accountApi.addTrustedAddress(appId, {
          address: entry.address,
          note: entry.note ?? "",
        });
        addedId = added.trusted_address.id;
      } catch (caught) {
        setError(
          `${entry.address} is no longer trusted. ${messageOf(caught, "It could not be added again.")} Add it below to trust it again.`,
        );
      }
    } catch (caught) {
      setError(messageOf(caught, "The address could not be changed. Try again."));
    }
    await queryClient.invalidateQueries({ queryKey: key });
    onChanged();
    setBusy(false);
    if (addedId !== null) {
      setKeptId(addedId);
      toast.success("Address added again");
    }
  }

  return (
    <Dialog
      open={app !== null}
      onOpenChange={(open) => {
        if (!open && !busy) {
          setError(null);
          onClose();
        }
      }}
    >
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Trusted addresses for {appName}</DialogTitle>
          <DialogDescription>
            A device at a trusted address can use this app with no key. It runs
            with this app&apos;s access plan, and its requests show the address.
            Add only addresses you control, such as one device or your home
            network. None are trusted until you add them.
          </DialogDescription>
        </DialogHeader>
        {isPending ? (
          <Skeleton className="h-10 w-full" />
        ) : isError ? (
          <p role="alert" className="text-destructive text-meta">
            The trusted addresses could not be loaded.
          </p>
        ) : data.length === 0 ? (
          <p className="text-muted-foreground text-meta">
            No address is trusted for this app. Requests without a key are
            refused.
          </p>
        ) : (
          <ul className="divide-y" aria-label="Trusted addresses">
            {data.map((entry) => (
              <li
                key={entry.id}
                className="flex items-center justify-between gap-3 py-2"
              >
                <span className="min-w-0">
                  <span className="font-mono">{entry.address}</span>
                  {entry.note ? (
                    <span className="text-muted-foreground text-meta block truncate">
                      {entry.note}
                    </span>
                  ) : null}
                  {entry.cutover === "keep" ? (
                    <span className="text-muted-foreground text-meta block">
                      Made before Azizam recorded who made addresses. Keep it, or
                      it stops working at the next ownership update.
                    </span>
                  ) : null}
                  {entry.cutover === "readd" ? (
                    <span className="text-muted-foreground text-meta block">
                      {entry.made_by_name
                        ? `Made by ${entry.made_by_name}.`
                        : "Made before Azizam recorded who made addresses."}{" "}
                      It stops working at the next ownership update. Add it again
                      yourself to keep it.
                    </span>
                  ) : null}
                </span>
                <div className="flex shrink-0 gap-2">
                  {entry.cutover === "keep" ? (
                    <Button
                      variant="outline"
                      disabled={busy}
                      aria-label={`Keep address ${entry.address}`}
                      onClick={() => void keep(entry)}
                    >
                      Keep
                    </Button>
                  ) : null}
                  {entry.cutover === "readd" ? (
                    <Button
                      variant="outline"
                      disabled={busy}
                      aria-label={`Add address ${entry.address} again`}
                      onClick={() => void addAgain(entry)}
                    >
                      Add again
                    </Button>
                  ) : null}
                  <Button
                    id={`remove-address-${entry.id}`}
                    variant="outline"
                    disabled={busy}
                    aria-label={`Remove ${entry.address}`}
                    onClick={() =>
                      void change(() =>
                        adminFetch(`${listPath(appId)}/${entry.id}`, {
                          method: "DELETE",
                        }),
                      )
                    }
                  >
                    Remove
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
        <form onSubmit={add} className="space-y-3">
          <div className="space-y-2">
            <label htmlFor="trusted-address" className="text-label">
              IP address or range
            </label>
            <Input
              id="trusted-address"
              required
              maxLength={64}
              placeholder="192.168.1.50 or 192.168.1.0/24"
              value={address}
              disabled={busy}
              onChange={(e) => setAddress(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <label htmlFor="trusted-note" className="text-label">
              Note (optional)
            </label>
            <Input
              id="trusted-note"
              maxLength={200}
              placeholder="Living room display"
              value={note}
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
            />
          </div>
          {error ? (
            <p role="alert" className="text-destructive text-meta">
              {error}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={onClose}
              disabled={busy}
            >
              Done
            </Button>
            <Button type="submit" disabled={busy || !address.trim()}>
              Trust address
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
