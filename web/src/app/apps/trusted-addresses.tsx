"use client";

import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { adminFetch } from "@/lib/api";
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
}

const listPath = (appId: string) =>
  `/admin/api/apps/${encodeURIComponent(appId)}/trusted-addresses`;

/** An owner trusts a device, or a range of them, to use one app with no
 * key. The app's access plan still limits what it can do. */
export function TrustedAddressesDialog({
  app,
  appName,
  onClose,
}: {
  app: { id: string } | null;
  appName: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [address, setAddress] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
                </span>
                <Button
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
