"use client";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  api,
  type Profile,
  type ProfilePolicyDocument,
  type PolicyEditorChoice,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { ErrorState } from "@/components/product";
const errorMessage = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The change could not be saved. Try again.";

function policyShapeError(value: unknown): string | null {
  const object = (v: unknown): v is Record<string, unknown> =>
    v !== null && typeof v === "object" && !Array.isArray(v);
  if (!object(value)) return "Enter a valid JSON object before saving.";
  const stringList = (v: unknown) =>
    Array.isArray(v) && v.every((item) => typeof item === "string");
  const lists: Record<string, string[]> = {
    "": ["models", "protocols", "workflows", "backends"],
    allow: ["intents", "exposure", "trust_tiers", "resource_tiers", "tags"],
    remote: ["providers"],
    native_passthrough: ["allowed_backends", "allowed_operations"],
  };
  for (const section of [
    "allow",
    "fallback",
    "remote",
    "native_passthrough",
    "status_visibility",
    "target_policy",
    "model_access",
  ]) {
    if (section in value && !object(value[section]))
      return `${section} must be an object.`;
  }
  for (const [section, fields] of Object.entries(lists)) {
    const record = section ? value[section] : value;
    if (!object(record)) continue;
    for (const field of fields) {
      if (field in record && !stringList(record[field]))
        return `${section ? `${section}.` : ""}${field} must be an array of strings.`;
    }
  }
  for (const [section, fields] of Object.entries({
    fallback: ["cloud_allowed"],
    remote: ["allow_paid_fallback"],
    native_passthrough: ["enabled", "audit"],
  })) {
    const record = value[section];
    if (!object(record)) continue;
    for (const field of fields) {
      if (field in record && typeof record[field] !== "boolean")
        return `${section}.${field} must be a boolean.`;
    }
  }
  return null;
}

function PolicySet({
  label,
  value,
  choices,
  change,
}: {
  label: string;
  value: string[] | undefined;
  choices: PolicyEditorChoice[];
  change: (value: string[] | undefined) => void;
}) {
  const unknown = (value || []).filter((v) => !choices.some((c) => c.id === v));
  return (
    <fieldset className="space-y-3">
      <legend className="text-sm font-medium">{label}</legend>
      <label className="flex min-h-11 items-center gap-3 text-sm">
        <Checkbox
          checked={value !== undefined}
          onCheckedChange={(checked) => change(checked ? [] : undefined)}
        />
        Restrict {label.toLowerCase()}
      </label>
      {value !== undefined && (
        <>
          <p className="text-muted-foreground text-xs">
            No selections denies all. Turn off the restriction to follow gateway
            defaults.
          </p>
          <div className="flex flex-wrap gap-x-5 gap-y-2">
            {choices.map((c) => (
              <label
                key={c.id}
                className="flex min-h-11 items-center gap-3 text-sm"
              >
                <Checkbox
                  checked={value.includes(c.id)}
                  disabled={c.available === false && !value.includes(c.id)}
                  onCheckedChange={(checked) =>
                    change(
                      checked
                        ? [...value, c.id]
                        : value.filter((v) => v !== c.id),
                    )
                  }
                />
                {c.label}
              </label>
            ))}
          </div>
          {unknown.length > 0 && (
            <p className="text-muted-foreground text-sm break-words">
              Other saved values (preserved): {unknown.join(", ")}
            </p>
          )}
          {!choices.length && (
            <p className="text-muted-foreground text-sm">
              No choices are registered yet.
            </p>
          )}
        </>
      )}
    </fieldset>
  );
}

export function PlanEditor({
  plan,
  close,
  saved,
  restoreFocus,
}: {
  plan: Profile | null;
  restoreFocus?: (event: Event) => void;
  close: () => void;
  saved: () => Promise<void>;
}) {
  const {
    data: editorData,
    isPending: editorIsPending,
    isError: editorIsError,
    refetch: editorRefetch,
  } = useQuery({
    queryKey: ["policy-editor"],
    queryFn: api.policyEditor.get,
  });
  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent
        onCloseAutoFocus={restoreFocus}
        className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl"
      >
        <DialogHeader>
          <DialogTitle>
            {plan ? "Edit access plan" : "New access plan"}
          </DialogTitle>
          <DialogDescription>
            Choose what this plan allows. Fields without a restriction use
            gateway defaults.
          </DialogDescription>
        </DialogHeader>
        {editorIsPending ? (
          <p role="status">Loading available choices…</p>
        ) : editorIsError ? (
          <ErrorState
            message="Available choices could not be loaded."
            onRetry={() => void editorRefetch()}
          />
        ) : (
          <PlanForm
            plan={plan}
            initial={plan?.profile || editorData.profile.create.profile}
            choices={editorData.profile.choices}
            close={close}
            saved={saved}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
function PlanForm({
  plan,
  initial,
  choices,
  close,
  saved,
}: {
  plan: Profile | null;
  initial: ProfilePolicyDocument;
  choices: Awaited<
    ReturnType<typeof api.policyEditor.get>
  >["profile"]["choices"];
  close: () => void;
  saved: () => Promise<void>;
}) {
  const [name, setName] = useState(plan?.name || "");
  const [description, setDescription] = useState(plan?.description || "");
  const [policy, setPolicy] = useState<ProfilePolicyDocument>(
    structuredClone(initial),
  );
  const [raw, setRaw] = useState<string | null>(null);
  const [rawError, setRawError] = useState("");
  const discardRaw = () => {
    setRaw(null);
    setRawError("");
  };
  const mutation = useMutation({
    mutationFn: () =>
      plan
        ? api.profiles.update(plan.id, {
            name: name.trim(),
            description,
            profile: policy,
          })
        : api.profiles.create({
            name: name.trim(),
            description,
            profile: policy,
          }),
    onSuccess: async () => {
      await saved();
      close();
    },
  });
  const setField = (
    field: "models" | "protocols" | "workflows" | "backends",
    value: string[] | undefined,
  ) => {
    setPolicy((old) => {
      const updated = { ...old };
      if (value === undefined) delete updated[field];
      else updated[field] = value;
      return updated;
    });
    discardRaw();
  };
  const setAllow = (
    field: "intents" | "exposure" | "trust_tiers" | "resource_tiers" | "tags",
    value: string[] | undefined,
  ) => {
    setPolicy((old) => {
      const allow = { ...old.allow };
      if (value === undefined) delete allow[field];
      else allow[field] = value;
      return { ...old, allow };
    });
    discardRaw();
  };
  return (
    <form
      className="space-y-6"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate();
      }}
    >
      <div className="space-y-2">
        <Label htmlFor="plan-name">Name</Label>
        <Input
          id="plan-name"
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={mutation.isPending}
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="plan-description">Description (optional)</Label>
        <Textarea
          id="plan-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>
      <PolicySet
        label="Capabilities"
        choices={choices.intents}
        value={policy.allow?.intents}
        change={(v) => setAllow("intents", v)}
      />
      <PolicySet
        label="Locations"
        choices={choices.exposure}
        value={policy.allow?.exposure}
        change={(v) => setAllow("exposure", v)}
      />
      <p className="text-muted-foreground text-xs">
        Remote access can incur provider charges. Cloud fallback is a separate
        permission under Advanced.
      </p>
      <details className="border-t pt-4">
        <summary className="cursor-pointer py-3 text-sm font-medium">
          Advanced
        </summary>
        <div className="space-y-6 pt-4">
          <PolicySet
            label="Models"
            choices={choices.models}
            value={policy.models}
            change={(v) => setField("models", v)}
          />
          <PolicySet
            label="Protocols"
            choices={choices.protocols}
            value={policy.protocols}
            change={(v) => setField("protocols", v)}
          />
          <PolicySet
            label="Workflows"
            choices={choices.workflows}
            value={policy.workflows}
            change={(v) => setField("workflows", v)}
          />
          <PolicySet
            label="Trust tiers"
            choices={choices.trust_tiers}
            value={policy.allow?.trust_tiers}
            change={(v) => setAllow("trust_tiers", v)}
          />
          <PolicySet
            label="Resource tiers"
            choices={choices.resource_tiers}
            value={policy.allow?.resource_tiers}
            change={(v) => setAllow("resource_tiers", v)}
          />
          <PolicySet
            label="Tags"
            choices={choices.tags}
            value={policy.allow?.tags}
            change={(v) => setAllow("tags", v)}
          />
          <fieldset>
            <legend className="text-sm font-medium">Cloud fallback</legend>
            <div className="flex flex-col gap-2 pt-2">
              {(["default", "allowed", "blocked"] as const).map((v) => (
                <label
                  className="flex min-h-11 items-center gap-3 text-sm"
                  key={v}
                >
                  <input
                    type="radio"
                    name="cloud-fallback"
                    checked={
                      v === "default"
                        ? policy.fallback?.cloud_allowed === undefined
                        : policy.fallback?.cloud_allowed === (v === "allowed")
                    }
                    onChange={() => {
                      setPolicy((old) => {
                        const fallback = { ...old.fallback };
                        if (v === "default") delete fallback.cloud_allowed;
                        else fallback.cloud_allowed = v === "allowed";
                        return { ...old, fallback };
                      });
                      discardRaw();
                    }}
                  />
                  {v === "default"
                    ? "Gateway default"
                    : v === "allowed"
                      ? "Allow cloud fallback"
                      : "Block cloud fallback"}
                </label>
              ))}
            </div>
          </fieldset>
          <details>
            <summary className="cursor-pointer py-3 text-sm font-medium">
              Complete policy document
            </summary>
            <p className="text-muted-foreground mb-3 text-xs">
              All saved fields are preserved. Edit this document for permissions
              without a form control.
            </p>
            <Label className="sr-only" htmlFor="policy-json">
              Policy JSON
            </Label>
            <Textarea
              id="policy-json"
              aria-invalid={!!rawError}
              aria-describedby={rawError ? "policy-json-error" : undefined}
              rows={12}
              className="font-mono text-xs"
              value={raw ?? JSON.stringify(policy, null, 2)}
              onChange={(e) => {
                setRaw(e.target.value);
                try {
                  const parsed = JSON.parse(e.target.value);
                  const shapeError = policyShapeError(parsed);
                  if (shapeError) {
                    setRawError(shapeError);
                    return;
                  }
                  setPolicy(parsed);
                  setRawError("");
                } catch {
                  setRawError("Enter a valid JSON object before saving.");
                }
              }}
            />
            {rawError && (
              <>
                <p
                  id="policy-json-error"
                  role="alert"
                  className="text-destructive text-sm"
                >
                  {rawError}
                </p>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={discardRaw}
                >
                  Discard invalid JSON changes
                </Button>
              </>
            )}
          </details>
        </div>
      </details>
      {mutation.isError && (
        <p role="alert" className="text-destructive text-sm">
          {errorMessage(mutation.error)}
        </p>
      )}
      <DialogFooter>
        {rawError && (
          <p
            id="plan-save-error"
            className="text-destructive self-center text-sm"
          >
            Save is blocked. Fix or discard invalid JSON changes in Advanced.
          </p>
        )}
        <Button
          type="button"
          variant="ghost"
          onClick={close}
          disabled={mutation.isPending}
        >
          Cancel
        </Button>
        <Button
          disabled={!name.trim() || !!rawError || mutation.isPending}
          aria-describedby={rawError ? "plan-save-error" : undefined}
        >
          {mutation.isPending ? "Saving…" : "Save access plan"}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function TestAccess({
  plan,
  close,
  restoreFocus,
}: {
  plan: Profile;
  restoreFocus?: (event: Event) => void;
  close: () => void;
}) {
  const [model, setModel] = useState("");
  const [intent, setIntent] = useState("");
  const [protocol, setProtocol] = useState("");
  const [exposure, setExposure] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      api.policy.simulate({
        profile: plan.id,
        request: {
          model: model.trim() || null,
          intent: intent.trim() || null,
          protocol: protocol.trim() || null,
          ...(exposure.trim() ? { capability_exposure: exposure.trim() } : {}),
        },
      }),
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && !mutation.isPending && close()}
    >
      <DialogContent onCloseAutoFocus={restoreFocus}>
        <DialogHeader>
          <DialogTitle>Test access · {plan.name}</DialogTitle>
          <DialogDescription>
            Simulate access without making a model request or incurring provider
            charges.
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            mutation.mutate();
          }}
        >
          <div className="space-y-2">
            <Label htmlFor="test-model">Model</Label>
            <Input
              id="test-model"
              value={model}
              onChange={(e) => {
                setModel(e.target.value);
                mutation.reset();
              }}
              placeholder="Model name or alias"
            />
          </div>
          <details>
            <summary className="cursor-pointer py-3 text-sm font-medium">
              Request details
            </summary>
            <div className="space-y-3">
              {[
                ["Capability / intent", intent, setIntent],
                ["Protocol", protocol, setProtocol],
                ["Location / exposure", exposure, setExposure],
              ].map(([label, value, set]) => (
                <div className="space-y-2" key={label as string}>
                  <Label htmlFor={`test-${label}`}>{label as string}</Label>
                  <Input
                    id={`test-${label}`}
                    value={value as string}
                    onChange={(e) => {
                      (set as (v: string) => void)(e.target.value);
                      mutation.reset();
                    }}
                  />
                </div>
              ))}
            </div>
          </details>
          {mutation.data && (
            <div role="status" className="rounded-lg border p-4">
              <p className="font-medium">
                {mutation.data.decision.allowed
                  ? "This request would be allowed"
                  : "This request would be denied"}
              </p>
              {mutation.data.decision.reasons.map((reason) => (
                <p
                  key={reason}
                  className="text-muted-foreground mt-2 text-sm break-words"
                >
                  {reason}
                </p>
              ))}
            </div>
          )}
          {mutation.isError && (
            <p role="alert" className="text-destructive text-sm">
              {errorMessage(mutation.error)}
            </p>
          )}
          <DialogFooter>
            <Button variant="ghost" type="button" onClick={close}>
              Close
            </Button>
            <Button disabled={mutation.isPending}>
              {mutation.isPending ? "Testing…" : "Run test"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
