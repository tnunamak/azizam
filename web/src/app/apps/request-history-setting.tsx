"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { accountApi, type AccountApp } from "@/lib/account-api";
import { periodWords, requestHistoryKey } from "@/lib/request-history";

export function RequestHistorySetting({
  app,
  onSaved,
}: {
  app: AccountApp;
  onSaved: (saved: AccountApp) => void;
}) {
  const [setting, setSetting] = useState(app.request_history);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The same answer the History page reads: the periods the gateway offers and the household's off state.
  const { data: policy, isError } = useQuery({
    queryKey: requestHistoryKey,
    queryFn: accountApi.requestHistory,
  });

  if (!setting) return null;

  async function save(next: { keep: boolean; retention: string }) {
    setBusy(true);
    setError(null);
    try {
      const saved = await accountApi.setRequestHistory(app.id, next);
      if (saved.request_history) setSetting(saved.request_history);
      onSaved(saved);
    } catch {
      setError("Could not save the request history setting. Try again.");
    } finally {
      setBusy(false);
    }
  }

  // Until the household's answer is known, nothing about the setting can be trusted: no active copy, no change.
  // A failed refetch counts as unknown too: the cached answer may no longer hold.
  const known = policy !== undefined && !isError;
  const householdOff = policy?.household_off === true;
  const choices = policy?.choices ?? [];
  const reader = setting.manager_name
    ? `Only ${setting.manager_name}, who manages this app, can read them in Request history.`
    : "Only the person who manages this app can read them in Request history.";
  const sentence = !known
    ? isError
      ? "The household setting could not be read. Try again later."
      : "Checking the household setting…"
    : householdOff
      ? "Off for the household: nothing new is kept."
      : setting.keep
        ? `Requests and replies from this app are kept for up to ${periodWords(setting.retention)}. ${reader} The household's limits can shorten this or stop it.`
        : "Requests from this app are not kept.";

  return (
    <div className="space-y-2 pr-4 pb-3 pl-11">
      <div className="flex flex-wrap items-center gap-3">
        <Switch
          id={`request-history-${app.id}`}
          aria-label="Keep request history"
          checked={known && setting.keep && !householdOff}
          disabled={busy || !known || householdOff}
          onCheckedChange={(keep) =>
            void save({ keep, retention: setting.retention })
          }
        />
        <Label htmlFor={`request-history-${app.id}`}>
          Keep request history
        </Label>
        <Select
          value={setting.retention}
          onValueChange={(retention) =>
            void save({ keep: setting.keep, retention })
          }
          disabled={busy || !known || householdOff || choices.length === 0}
        >
          <SelectTrigger aria-label="Keep request history for" className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {choices.map((id) => (
              <SelectItem key={id} value={id}>
                {periodWords(id)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <p className="text-muted-foreground text-sm">{sentence}</p>
      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}
    </div>
  );
}
