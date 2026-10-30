"use client";

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
import { periodWords } from "@/lib/request-history";

// The gateway checks the same ids (registry/retention.yml, request_history choices).
const PERIODS = ["1_day", "7_days", "30_days"];

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

  const sentence = setting.keep
    ? `Requests from this app are kept for ${periodWords(setting.retention)}. ${setting.manager_name} can read them in Request history.`
    : "Requests from this app are not kept.";

  return (
    <div className="space-y-2 pr-4 pb-3 pl-11">
      <div className="flex flex-wrap items-center gap-3">
        <Switch
          id={`request-history-${app.id}`}
          aria-label="Keep request history"
          checked={setting.keep}
          disabled={busy}
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
          disabled={busy}
        >
          <SelectTrigger aria-label="Keep request history for" className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PERIODS.map((id) => (
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
