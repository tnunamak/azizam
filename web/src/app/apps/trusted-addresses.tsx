"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { adminFetch } from "@/lib/api";
import {
  accountApi,
  administratorsToConfirm,
  hasAdministrators,
  type AdministratorsOnRecord,
  type KeepOptions,
} from "@/lib/account-api";
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
import { KeepConfirmDialog, type KeepConfirmation } from "./keep-confirm";

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
  /** The administrators on record who could also have added it. Keeping it asks the owner to confirm them first. */
  administrators?: AdministratorsOnRecord;
  /** The person recorded as its maker, when that is another person. */
  made_by_name?: string;
}

const listPath = (appId: string) =>
  `/admin/api/apps/${encodeURIComponent(appId)}/trusted-addresses`;

/** One app's trusted addresses. The App's section on the Apps page and its dialog share this cache entry. */
export const trustedAddressesKey = (appId: string) => ["admin", "app-trusted-addresses", appId];

export function listTrustedAddresses(appId: string): Promise<TrustedAddress[]> {
  return adminFetch<{ trusted_addresses: TrustedAddress[] }>(listPath(appId)).then(
    (r) => r.trusted_addresses,
  );
}

/** What to show for a failed change: the gateway's words, or the fallback. */
function messageOf(caught: unknown, fallback: string): string {
  return caught instanceof Error && caught.message ? caught.message : fallback;
}

/** The row of the first address that needs a remedy, if the list is on the page. */
function checkRowOf(data: TrustedAddress[] | undefined): HTMLElement | null {
  const entry = data?.find((item) => item.cutover !== undefined);
  return entry ? document.getElementById(`address-row-${entry.id}`) : null;
}

function focusRow(row: HTMLElement) {
  row.scrollIntoView({ block: "center" });
  row.querySelector("button")?.focus();
}

/** An owner trusts a device, or a range of them, to use one app with no
 * key. The app's access plan still limits what it can do. */
export function TrustedAddressesDialog({
  app,
  appName,
  focusCheck = false,
  onClose,
  onChanged,
}: {
  app: { id: string } | null;
  appName: string;
  /** Opened from the page's quick-check line: the first address with a remedy is brought into view and focused. */
  focusCheck?: boolean;
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
  const [keepConfirmation, setKeepConfirmation] = useState<KeepConfirmation | null>(null);
  const appId = app?.id ?? "";
  const key = trustedAddressesKey(appId);
  const { data, isPending, isError } = useQuery({
    queryKey: key,
    enabled: app !== null,
    queryFn: () => listTrustedAddresses(appId),
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

  // Opened from the quick-check line: the first address with a remedy is brought into view and focused, once per
  // opening. The list may already be cached (the App's section on the page loads it), so the row can be on screen
  // when the dialog opens, or arrive after it. The open-focus handler covers the first case: it runs once the
  // dialog's content is mounted, and Radix's own first-focus would otherwise win.
  const checkFocused = useRef(false);
  useEffect(() => {
    if (app === null) checkFocused.current = false;
    if (!focusCheck || checkFocused.current || !data) return;
    const row = checkRowOf(data);
    if (!row) return;
    checkFocused.current = true;
    focusRow(row);
  }, [app, data, focusCheck]);

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

  // A keep that administrators on record could also have made waits for the owner's confirmation. The gateway may ask
  // for it too, when the list this dialog holds is stale.
  async function keep(entry: TrustedAddress) {
    if (entry.administrators && hasAdministrators(entry.administrators)) {
      setKeepConfirmation({ id: entry.id, what: "address", administrators: entry.administrators });
      return;
    }
    await keepNow(entry.id, {});
  }

  async function keepNow(entryId: string, options: KeepOptions) {
    setBusy(true);
    setError(null);
    try {
      await accountApi.keepTrustedAddress(appId, entryId, options);
      await queryClient.invalidateQueries({ queryKey: key });
      setKeptId(entryId);
      onChanged();
      toast.success("Address kept");
    } catch (caught) {
      const administrators = administratorsToConfirm(caught);
      if (administrators !== null) {
        setKeepConfirmation({ id: entryId, what: "address", administrators });
      } else {
        setError(messageOf(caught, "The address could not be kept. Try again."));
      }
    } finally {
      setBusy(false);
    }
  }

  // The owner confirmed the administrators. A failure rejects, so the dialog stays open with the reason in it.
  async function confirmKeep(confirmation: KeepConfirmation) {
    try {
      await accountApi.keepTrustedAddress(appId, confirmation.id, { confirmed: true });
    } catch (caught) {
      const administrators = administratorsToConfirm(caught);
      setKeepConfirmation({
        ...confirmation,
        administrators: administrators ?? confirmation.administrators,
        error:
          administrators !== null
            ? "The administrators changed. Read them again, then keep it."
            : messageOf(caught, "The address could not be kept. Try again."),
      });
      throw caught;
    }
    await queryClient.invalidateQueries({ queryKey: key });
    setKeptId(confirmation.id);
    onChanged();
    toast.success("Address kept");
  }

  // One call: the gateway replaces the old entry with the same address, made by this person, in one transaction. A
  // failure leaves the old entry in place, so the list still shows it. Focus moves to the new entry's Remove button.
  async function addAgain(entry: TrustedAddress) {
    setBusy(true);
    setError(null);
    try {
      const readded = await accountApi.readdTrustedAddress(appId, entry.id);
      await queryClient.invalidateQueries({ queryKey: key });
      setKeptId(readded.trusted_address.id);
      onChanged();
      toast.success("Address added again");
    } catch (caught) {
      setError(messageOf(caught, "The address could not be added again. Try again."));
    } finally {
      setBusy(false);
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
      <DialogContent
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onOpenAutoFocus={(event) => {
          if (!focusCheck || checkFocused.current) return;
          const row = checkRowOf(data);
          if (!row) return;
          event.preventDefault();
          checkFocused.current = true;
          focusRow(row);
        }}
      >
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
                id={`address-row-${entry.id}`}
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
        <KeepConfirmDialog
          confirmation={keepConfirmation}
          onClose={() => setKeepConfirmation(null)}
          onConfirm={async () => {
            if (keepConfirmation) await confirmKeep(keepConfirmation);
          }}
          fallbackFocus={() => [document.getElementById(`remove-address-${keepConfirmation?.id ?? ""}`)]}
        />
      </DialogContent>
    </Dialog>
  );
}
