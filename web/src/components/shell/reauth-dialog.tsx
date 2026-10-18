"use client";

import { useRef, useState } from "react";
import { ApiError, confirmWithPassword } from "@/lib/api";
import { navigate } from "@/lib/navigate";
import { reauthInvoker, settleReauth, type ReauthInfo } from "@/lib/reauth";
import { restoreFocus } from "@/lib/restore-focus";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";

function currentPath(): string {
  return window.location.pathname + window.location.search;
}

/** Where the identity provider sends the person back to: this page. */
function providerUrl(oidcUrl: string): string {
  const joiner = oidcUrl.includes("?") ? "&" : "?";
  return `${oidcUrl}${joiner}return_to=${encodeURIComponent(currentPath())}`;
}

/** The shared "Confirm it's you" prompt (lib/reauth.ts). Open while a risky
 * action waits for the person to prove who they are. */
export function ReauthDialog({ info }: { info: ReauthInfo | null }) {
  // Keep the last details so the closing animation still has its content.
  const [shown, setShown] = useState(info);
  if (info && info !== shown) setShown(info);
  return (
    <Dialog
      open={info !== null}
      onOpenChange={(open) => {
        if (!open) settleReauth(false);
      }}
    >
      {shown && (
        <DialogContent
          showCloseButton={false}
          className="flex! max-h-[85dvh] flex-col gap-0! overflow-y-auto sm:max-w-md"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            restoreFocus(reauthInvoker());
          }}
        >
          <ReauthForm info={shown} />
        </DialogContent>
      )}
    </Dialog>
  );
}

function ReauthForm({ info }: { info: ReauthInfo }) {
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const hasPassword = info.methods.includes("password");
  const hasProvider = info.methods.includes("oidc");
  const signInOnly = !hasPassword && !hasProvider;

  async function confirm() {
    if (pending || !password) return;
    setPending(true);
    setMessage(null);
    try {
      await confirmWithPassword(password);
      settleReauth(true);
    } catch (error) {
      // A wrong password keeps the prompt open; nothing else changes.
      setMessage(
        error instanceof Error
          ? error.message
          : "We couldn’t confirm that. Try again.",
      );
      setPassword("");
      if (!(error instanceof ApiError && error.status >= 500)) {
        requestAnimationFrame(() => input.current?.focus());
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <form
      className="flex min-h-0 flex-col"
      onSubmit={(event) => {
        event.preventDefault();
        void confirm();
      }}
    >
      <DialogHeader className="shrink-0 gap-1.5 pr-8 text-left">
        <DialogTitle>Confirm it’s you</DialogTitle>
        <DialogDescription>
          This action needs a recent sign-in.
        </DialogDescription>
      </DialogHeader>
      {hasPassword && (
        <div className="mt-4">
          <Field
            label="Password"
            error={message ? <span role="alert">{message}</span> : undefined}
          >
            <Input
              ref={input}
              type="password"
              autoComplete="current-password"
              autoFocus
              value={password}
              disabled={pending}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
        </div>
      )}
      {hasPassword && hasProvider && (
        <Button
          type="button"
          variant="outline"
          size="md"
          className="mt-4"
          disabled={pending}
          onClick={() => navigate.to(providerUrl(info.oidcUrl))}
        >
          Continue with your identity provider
        </Button>
      )}
      <DialogFooter className="mt-6 shrink-0">
        <Button
          size="md"
          type="button"
          variant="ghost"
          onClick={() => settleReauth(false)}
          disabled={pending}
        >
          Cancel
        </Button>
        {hasPassword && (
          <Button
            size="md"
            type="submit"
            aria-busy={pending || undefined}
            disabled={pending || !password}
          >
            {pending && <Spinner className="size-4" />}
            Confirm
          </Button>
        )}
        {!hasPassword && hasProvider && (
          <Button
            size="md"
            type="button"
            autoFocus
            onClick={() => navigate.to(providerUrl(info.oidcUrl))}
          >
            Continue with your identity provider
          </Button>
        )}
        {signInOnly && (
          <Button
            size="md"
            type="button"
            autoFocus
            onClick={() =>
              navigate.to(
                `/sign-in/?return_to=${encodeURIComponent(currentPath())}`,
              )
            }
          >
            Sign in again
          </Button>
        )}
      </DialogFooter>
    </form>
  );
}
