"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { adminFetch, ApiError } from "@/lib/api";
import { useViewer } from "@/components/shell/session";
import { SettingsSection } from "@/components/product/setting-row";
import { ErrorState } from "@/components/product/error-state";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { toast } from "@/lib/toast";

const sessionsKey = ["settings", "sessions"] as const;

interface SessionPolicy {
  idle_timeout_days: number;
  absolute_lifetime_days: number;
  step_up_minutes: number;
}
type PolicyKey = keyof SessionPolicy;
interface SessionsResponse {
  sessions: SessionPolicy;
  defaults: SessionPolicy;
  bounds: Record<PolicyKey, [number, number]>;
  revision: string;
}

const fields: {
  key: PolicyKey;
  label: string;
  unit: "days" | "minutes";
}[] = [
  {
    key: "idle_timeout_days",
    label: "Sign out after this many days without use",
    unit: "days",
  },
  {
    key: "absolute_lifetime_days",
    label: "Always sign in again after this many days",
    unit: "days",
  },
  {
    key: "step_up_minutes",
    label: "Ask for the password again for risky actions after this many minutes",
    unit: "minutes",
  },
];

export function SessionsSection() {
  const owner = useViewer().role === "owner";
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: sessionsKey,
    queryFn: () => adminFetch<SessionsResponse>("/admin/api/settings/sessions"),
    enabled: owner,
  });
  return (
    <SettingsSection
      id="sign-in-sessions"
      title="Sign-in sessions"
      description="How long a sign-in lasts, and when a risky action asks you to confirm it’s you."
    >
      {!owner ? (
        <p className="text-muted-foreground text-meta p-4">
          Only the owner can change this.
        </p>
      ) : isPending ? (
        <p role="status" className="text-muted-foreground text-meta p-4">
          Loading sign-in session settings…
        </p>
      ) : isError ? (
        <div className="p-4">
          <ErrorState
            heading="Couldn’t load sign-in session settings"
            error={error}
            onRetry={() => void refetch()}
          />
        </div>
      ) : (
        <SessionsForm key={data.revision} initial={data} />
      )}
    </SettingsSection>
  );
}

function saveMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 409)
    return "Settings changed elsewhere; reload to see the latest";
  if (error instanceof ApiError && error.status === 403)
    return "Only the owner can change this.";
  return error instanceof Error && error.message
    ? error.message
    : "The settings weren’t saved. Try again.";
}

function SessionsForm({ initial }: { initial: SessionsResponse }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Record<PolicyKey, string>>(() => ({
    idle_timeout_days: String(initial.sessions.idle_timeout_days),
    absolute_lifetime_days: String(initial.sessions.absolute_lifetime_days),
    step_up_minutes: String(initial.sessions.step_up_minutes),
  }));
  const [notWhole, setNotWhole] = useState<PolicyKey | null>(null);
  const changed = fields.some(
    ({ key }) => draft[key] !== String(initial.sessions[key]),
  );
  const save = useMutation({
    mutationFn: (body: SessionPolicy) =>
      adminFetch<SessionsResponse>("/admin/api/settings/sessions", {
        method: "PUT",
        body,
        headers: { "If-Match": initial.revision },
      }),
    onSuccess: (data) => {
      client.setQueryData(sessionsKey, data);
      void client.invalidateQueries({ queryKey: ["settings", "effective"] });
      toast.success("Sign-in session settings saved");
    },
  });
  function submit() {
    // The gateway owns the limits and says what is wrong; here only a
    // number that is not whole stops the request.
    const body = {} as SessionPolicy;
    for (const { key } of fields) {
      const value = Number(draft[key]);
      if (draft[key].trim() === "" || !Number.isInteger(value)) {
        setNotWhole(key);
        return;
      }
      body[key] = value;
    }
    setNotWhole(null);
    save.mutate(body);
  }
  return (
    <form
      noValidate
      className="space-y-5 p-4 sm:p-6"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      {fields.map(({ key, label, unit }) => {
        const [min, max] = initial.bounds[key];
        return (
          <Field
            key={key}
            label={label}
            help={`Whole ${unit}, from ${min.toLocaleString()} to ${max.toLocaleString()}. Usually ${initial.defaults[key].toLocaleString()}.`}
            error={notWhole === key ? "Enter a whole number." : undefined}
          >
            <Input
              type="number"
              inputMode="numeric"
              min={min}
              max={max}
              step={1}
              className="sm:max-w-40"
              value={draft[key]}
              disabled={save.isPending}
              onChange={(event) =>
                setDraft({ ...draft, [key]: event.target.value })
              }
            />
          </Field>
        );
      })}
      {save.isError && (
        <p role="alert" className="text-destructive text-meta">
          <span className="font-medium">Unsaved changes. </span>
          {saveMessage(save.error)}
        </p>
      )}
      <Button
        type="submit"
        className="h-auto! min-h-11 max-w-full py-2 whitespace-normal!"
        disabled={!changed || save.isPending}
      >
        {save.isPending ? "Saving…" : "Save sign-in session settings"}
      </Button>
    </form>
  );
}
