"use client";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { CopyField } from "@/components/product/copy-field";
import { SettingRow } from "@/components/product/setting-row";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/lib/toast";
import { setOwnerPassword, type SignInSnapshot } from "@/lib/signin-api";
import { Block, StatusDot, useReadOnly } from "./bits";
import {
  hasPasswordText,
  lockedReasonPassword,
  lockedReasonProvider,
  noPasswordText,
  providerRowDescription,
  rowLabel,
  type ProviderLabel,
  type Status,
} from "./copy";

const WHY =
  "text-label text-muted-foreground max-w-64 text-right max-sm:max-w-none max-sm:basis-full max-sm:text-left";

export function WaysToSignIn({
  label,
  password,
  provider,
  providerStatus,
  hasAddress,
  canTurnOnProvider,
  owner,
  recoveryCommand,
  onPassword,
  onProvider,
  onSnapshot,
}: {
  label: ProviderLabel;
  password: boolean;
  provider: boolean;
  providerStatus: Status;
  hasAddress: boolean;
  /** A test passed for this connection, or it is already on. */
  canTurnOnProvider: boolean;
  owner: SignInSnapshot["owner"];
  recoveryCommand: string;
  onPassword: (on: boolean) => void;
  onProvider: (on: boolean) => void;
  onSnapshot: (snapshot: SignInSnapshot) => void;
}) {
  const readOnly = useReadOnly();
  // The two ways can never both be off: the last one on is locked.
  const passwordLocked = password && !provider;
  const providerLastOn = provider && !password;
  const providerLocked = providerLastOn || (!provider && !canTurnOnProvider);
  const passwordWhy = passwordLocked ? lockedReasonPassword(label) : "";
  const providerWhy = providerLastOn ? lockedReasonProvider : "";

  return (
    <Block title="Ways to sign in" id="sign-in-ways">
      <div className="divide-y">
        <SettingRow
          wrapDescription
          controlClassName="flex-wrap justify-end max-sm:w-full max-sm:justify-between"
          label="Email and password"
          description="For people you invite by email. You, the owner, can always use your password."
          control={
            <>
              {passwordWhy && (
                <span id="sign-in-password-why" className={WHY}>
                  {passwordWhy}
                </span>
              )}
              <StatusDot
                status={{
                  tone: password ? "ok" : "off",
                  text: password ? "On" : "Off",
                }}
              />
              <Switch
                aria-label="Email and password"
                aria-describedby={
                  passwordWhy ? "sign-in-password-why" : undefined
                }
                aria-disabled={passwordLocked || undefined}
                disabled={readOnly}
                className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
                checked={password}
                onCheckedChange={(on) => !passwordLocked && onPassword(on)}
              />
            </>
          }
        />
        <SettingRow
          wrapDescription
          controlClassName="flex-wrap justify-end max-sm:w-full max-sm:justify-between"
          label={rowLabel(label)}
          description={providerRowDescription({ hasAddress, p: label })}
          control={
            <>
              {providerWhy && (
                <span id="sign-in-provider-why" className={WHY}>
                  {providerWhy}
                </span>
              )}
              <StatusDot status={providerStatus} id="sign-in-provider-status" />
              <Switch
                aria-label={label.name || "Your provider"}
                aria-describedby={
                  providerWhy
                    ? "sign-in-provider-why"
                    : "sign-in-provider-status"
                }
                aria-disabled={providerLocked || undefined}
                disabled={readOnly}
                className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
                checked={provider}
                onCheckedChange={(on) => !providerLocked && onProvider(on)}
              />
            </>
          }
        />
        <Recovery
          label={label}
          owner={owner}
          command={recoveryCommand}
          onSnapshot={onSnapshot}
        />
      </div>
    </Block>
  );
}

function Recovery({
  label,
  owner,
  command,
  onSnapshot,
}: {
  label: ProviderLabel;
  owner: SignInSnapshot["owner"];
  command: string;
  onSnapshot: (snapshot: SignInSnapshot) => void;
}) {
  const readOnly = useReadOnly();
  const [setting, setSetting] = useState(false);
  const [password, setPassword] = useState("");
  const save = useMutation({
    mutationFn: () => setOwnerPassword(password),
    onSuccess: (snapshot) => {
      setPassword("");
      setSetting(false);
      onSnapshot(snapshot);
      toast.success("Password set.");
    },
  });
  return (
    <div>
      <div className="flex min-h-(--row-setting) flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-2.5">
        <p className="text-body min-w-0 flex-1 basis-60 font-medium">
          {owner.has_password ? hasPasswordText(label) : noPasswordText(label)}
        </p>
        {!owner.has_password && !readOnly && !setting && (
          <Button variant="outline" onClick={() => setSetting(true)}>
            Set a password
          </Button>
        )}
      </div>
      {setting && !owner.has_password && (
        <form
          className="flex flex-wrap items-end gap-3 px-4 pb-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (password) save.mutate();
          }}
        >
          <div className="min-w-0 flex-1 basis-56 space-y-1.5">
            <Label htmlFor="sign-in-owner-password">New password</Label>
            <Input
              id="sign-in-owner-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <Button type="submit" disabled={!password || save.isPending}>
            {save.isPending ? "Saving…" : "Save password"}
          </Button>
          <Button variant="ghost" onClick={() => setSetting(false)}>
            Cancel
          </Button>
          {save.error && (
            <p role="alert" className="text-destructive text-meta basis-full">
              {save.error instanceof Error
                ? save.error.message
                : "The password could not be saved."}
            </p>
          )}
        </form>
      )}
      <div className="px-4 pb-4">
        <CopyField
          label="Locked out? Run this on the Azizam host, then paste the key at /setup/"
          buttonLabel="Copy recovery command"
          value={command}
        />
      </div>
    </div>
  );
}
