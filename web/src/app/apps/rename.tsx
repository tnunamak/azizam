"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { accountApi, type AccountApp } from "@/lib/account-api";
import type { Credential } from "@/lib/api";
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
import { RelativeTime } from "@/components/product";

/** The most characters a name may have; the gateway enforces the same limit. */
const NAME_LIMIT = 80;

/** What to show when a rename fails. Gateway messages are plain words. */
function failure(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

/** The name to send, or null when it is empty, too long, or already the saved one. */
function nameToSave(draft: string, saved: string): string | null {
  const trimmed = draft.trim();
  if (!trimmed || trimmed.length > NAME_LIMIT || trimmed === saved) return null;
  return trimmed;
}

/** First characters of a key, enough to tell keys apart and never enough to use one. */
function keyStart(prefix: string): string {
  return `${prefix.slice(0, 8)}…`;
}

function RenameAppForm({
  app,
  onSaved,
  onCancel,
}: {
  app: AccountApp;
  onSaved: (app: AccountApp) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(app.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const next = nameToSave(draft, app.name);
  // Edit the stored name, so what is selected is what will change.
  useEffect(() => input.current?.select(), []);
  // After a refusal the person edits the name again.
  useEffect(() => {
    if (error) input.current?.focus();
  }, [error]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (next === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      onSaved(await accountApi.renameApp(app.id, next));
    } catch (caught) {
      setError(failure(caught, "The app could not be renamed. Try again."));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={save} className="space-y-5">
      <div className="space-y-2">
        <label htmlFor="rename-app-name" className="text-label">
          App name
        </label>
        <Input
          ref={input}
          id="rename-app-name"
          autoFocus
          required
          maxLength={NAME_LIMIT}
          value={draft}
          readOnly={busy}
          aria-invalid={error ? true : undefined}
          aria-describedby={
            error ? "rename-app-hint rename-app-error" : "rename-app-hint"
          }
          onChange={(event) => {
            setDraft(event.target.value);
            setError(null);
          }}
        />
        <p id="rename-app-hint" className="text-muted-foreground text-meta">
          Up to {NAME_LIMIT} characters.
        </p>
      </div>
      {error && (
        <p
          id="rename-app-error"
          role="alert"
          className="text-destructive text-meta"
        >
          {error}
        </p>
      )}
      <DialogFooter>
        <Button variant="outline" type="button" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" disabled={busy || next === null}>
          {busy ? "Saving…" : "Save"}
        </Button>
      </DialogFooter>
    </form>
  );
}

/** Change an app's name. The saved record from the gateway comes back through
 * `onSaved`; the name is never shown before the gateway has accepted it. */
export function RenameAppDialog({
  app,
  appName,
  onSaved,
  onClose,
  onCloseAutoFocus,
}: {
  app: AccountApp | null;
  /** The name as the page shows it. */
  appName: string;
  onSaved: (app: AccountApp) => void;
  onClose: () => void;
  onCloseAutoFocus: (event: Event) => void;
}) {
  return (
    <Dialog open={app !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle className="break-words [overflow-wrap:anywhere]">
            Rename {appName}
          </DialogTitle>
          <DialogDescription>
            Pick a name you will recognize. Keys keep working.
          </DialogDescription>
        </DialogHeader>
        {app && <RenameAppForm app={app} onSaved={onSaved} onCancel={onClose} />}
      </DialogContent>
    </Dialog>
  );
}

function KeyRow({
  keyRecord,
  editing,
  disabled,
  onEdit,
  onSaved,
  onCancel,
}: {
  keyRecord: Credential;
  editing: boolean;
  /** Another row is being saved or edited. */
  disabled: boolean;
  onEdit: () => void;
  onSaved: (key: Credential) => void;
  onCancel: () => void;
}) {
  const label = keyRecord.display_name;
  const [draft, setDraft] = useState(label);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const next = nameToSave(draft, label);
  const errorId = `key-error-${keyRecord.id}`;

  useEffect(() => {
    if (!editing) return;
    setDraft(label);
    setError(null);
    input.current?.focus();
    input.current?.select();
    // Start from the saved name each time an edit opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);
  // After a refusal the person edits the name again.
  useEffect(() => {
    if (error) input.current?.focus();
  }, [error]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (next === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      onSaved(await accountApi.renameKey(keyRecord.id, next));
    } catch (caught) {
      setError(failure(caught, "The key could not be renamed. Try again."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="space-y-2 py-3">
      {editing ? (
        <form onSubmit={save} className="space-y-2">
          <label htmlFor={`key-name-${keyRecord.id}`} className="text-label">
            Key name
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <Input
              ref={input}
              id={`key-name-${keyRecord.id}`}
              required
              maxLength={NAME_LIMIT}
              value={draft}
              readOnly={busy}
              className="min-w-0 flex-1 basis-48"
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? errorId : undefined}
              onChange={(event) => {
                setDraft(event.target.value);
                setError(null);
              }}
            />
            <Button type="submit" disabled={busy || next === null}>
              {busy ? "Saving…" : "Save"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={onCancel}
            >
              Cancel
            </Button>
          </div>
        </form>
      ) : (
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p
              className="text-body font-medium break-words [overflow-wrap:anywhere]"
              title={label}
            >
              {label}
            </p>
            <p className="text-muted-foreground text-meta">
              <span className="font-mono">{keyStart(keyRecord.prefix)}</span>
              {" · "}
              {keyRecord.last_used_at != null ? (
                <>
                  Last used <RelativeTime ts={keyRecord.last_used_at} />
                </>
              ) : (
                "Never used"
              )}
            </p>
          </div>
          <Button
            id={`rename-key-${keyRecord.id}`}
            variant="outline"
            disabled={disabled}
            aria-label={`Rename key ${label}`}
            onClick={onEdit}
          >
            Rename
          </Button>
        </div>
      )}
      {error && (
        <p id={errorId} role="alert" className="text-destructive text-meta">
          {error}
        </p>
      )}
    </li>
  );
}

/** One app's keys, each with a Rename button. One key is edited at a time. */
export function RenameKeysDialog({
  app,
  appName,
  onSaved,
  onClose,
  onCloseAutoFocus,
}: {
  app: AccountApp | null;
  appName: string;
  onSaved: (key: Credential) => void;
  onClose: () => void;
  onCloseAutoFocus: (event: Event) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  // The row that had the edit, so its Rename button gets focus back.
  const returnTo = useRef<string | null>(null);

  useEffect(() => {
    if (editing === null && returnTo.current) {
      document.getElementById(`rename-key-${returnTo.current}`)?.focus();
      returnTo.current = null;
    }
  }, [editing]);

  function stopEditing(id: string) {
    returnTo.current = id;
    setEditing(null);
  }

  function close() {
    setEditing(null);
    onClose();
  }

  return (
    <Dialog open={app !== null} onOpenChange={(open) => !open && close()}>
      <DialogContent
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onCloseAutoFocus={onCloseAutoFocus}
        // Esc leaves a rename first; a second Esc closes the dialog.
        onEscapeKeyDown={(event) => {
          if (editing === null) return;
          event.preventDefault();
          stopEditing(editing);
        }}
      >
        <DialogHeader>
          <DialogTitle className="break-words [overflow-wrap:anywhere]">
            Keys for {appName}
          </DialogTitle>
          <DialogDescription>
            A new name does not change the key. Apps that use it keep working.
          </DialogDescription>
        </DialogHeader>
        {app && app.credentials.length === 0 ? (
          <p className="text-muted-foreground text-meta">
            This app has no keys.
          </p>
        ) : (
          <ul className="divide-y" aria-label="Keys">
            {app?.credentials.map((keyRecord) => (
              <KeyRow
                key={keyRecord.id}
                keyRecord={keyRecord}
                editing={editing === keyRecord.id}
                disabled={editing !== null && editing !== keyRecord.id}
                onEdit={() => setEditing(keyRecord.id)}
                onSaved={(saved) => {
                  onSaved(saved);
                  stopEditing(keyRecord.id);
                }}
                onCancel={() => stopEditing(keyRecord.id)}
              />
            ))}
          </ul>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={close}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
