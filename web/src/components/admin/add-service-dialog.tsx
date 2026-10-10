"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, ApiError, queryKeys, type ServiceRegistry } from "@/lib/api";
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
import { EmptyOperationalState, IncidentBanner } from "@/components/ui/operational";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { Spinner } from "@/components/ui/spinner";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type ServiceFormState = {
  name: string;
  preset: string;
  base_url: string;
  api_key_env: string;
};

type TestProbe = {
  ok: boolean;
  status: number | null;
  error: string;
  url: string;
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 412) {
      return "The service registry changed elsewhere (412 Precondition Failed). Refresh and retry.";
    }
    return error.message;
  }
  return "Something went wrong. Please try again.";
}

function emptyFormState(): ServiceFormState {
  return { name: "", preset: "", base_url: "", api_key_env: "" };
}

function formToApiBody(state: ServiceFormState): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: state.name.trim(),
    preset: state.preset,
    base_url: state.base_url.trim(),
  };
  if (state.api_key_env.trim()) body.api_key_env = state.api_key_env.trim();
  return body;
}

export function AddServiceDialog({
  open,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <AddServiceDialogContent
          onOpenChange={onOpenChange}
          onSaved={onSaved}
        />
      )}
    </Dialog>
  );
}

/** A component boundary so the add workflow can be mounted without a Next page. */
export function AddServiceDialogContent({
  onOpenChange,
  onSaved,
}: {
  onOpenChange: (open: boolean) => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  const returnFocus = useRef<HTMLElement | null>(
    typeof document === "undefined"
      ? null
      : (document.activeElement as HTMLElement),
  );
  const {
    data,
    error,
    isPending: loading,
    refetch,
  } = useQuery({
    queryKey: queryKeys.servicePresets,
    queryFn: api.services.presets,
  });
  const presets = data?.presets ?? [];
  const [form, setForm] = useState<ServiceFormState>(emptyFormState);
  const [formError, setFormError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<
    Partial<Record<"name" | "base_url" | "preset", string>>
  >({});
  const [probe, setProbe] = useState<TestProbe | null>(null);
  const [testing, setTesting] = useState(false);
  const errorSummaryRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (formError) errorSummaryRef.current?.focus();
  }, [formError]);

  const save = useMutation({
    mutationFn: () => api.services.add(formToApiBody(form)),
    onSuccess: onSaved,
    onError: (err) => setFormError(errorMessage(err)),
  });

  function set(key: keyof ServiceFormState) {
    return (event: React.ChangeEvent<HTMLInputElement>) =>
      setForm((current) => ({ ...current, [key]: event.target.value }));
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    const next: Partial<Record<"name" | "base_url" | "preset", string>> = {};
    if (!form.name.trim()) next.name = "Enter an engine name.";
    if (!form.base_url.trim())
      next.base_url = "Enter a base URL including its scheme.";
    if (!presets.some((preset) => preset.id === form.preset)) {
      next.preset = "Select an available engine preset.";
    }
    setFieldErrors(next);
    if (Object.keys(next).length) {
      setFormError("Fix the highlighted fields before adding the engine.");
      return;
    }
    save.mutate();
  }

  async function handleTest() {
    setTesting(true);
    setProbe(null);
    try {
      setProbe(
        await api.services.testConnection({ base_url: form.base_url.trim() }),
      );
    } catch (err) {
      toast.error("Test failed", { description: errorMessage(err) });
    } finally {
      setTesting(false);
    }
  }

  if (loading) {
    return (
      <DialogContent
        className="flex max-h-[85dvh] flex-col gap-0 overflow-hidden"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returnFocus.current?.focus();
        }}
      >
        <DialogHeader className="shrink-0 gap-1.5 pr-8 text-left">
          <DialogTitle>Add engine</DialogTitle>
          <DialogDescription>
            Loading the presets required to configure this engine.
          </DialogDescription>
        </DialogHeader>
        <div
          data-slot="dialog-body"
          className="mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto -mx-6 px-6 [mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)]"
        >
          <div className="flex flex-col gap-3" aria-busy="true">
            <Skeleton aria-hidden="true" className="h-10 w-full" />
            <Skeleton aria-hidden="true" className="h-10 w-full" />
            <Skeleton aria-hidden="true" className="h-10 w-full" />
          </div>
        </div>
      </DialogContent>
    );
  }
  if (error) {
    return (
      <DialogContent
        className="flex max-h-[85dvh] flex-col gap-0 overflow-hidden"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returnFocus.current?.focus();
        }}
      >
        <DialogHeader className="shrink-0 gap-1.5 pr-8 text-left">
          <DialogTitle>Add engine</DialogTitle>
          <DialogDescription>
            Engine presets could not be loaded. No preset has been selected.
          </DialogDescription>
        </DialogHeader>
        <div
          data-slot="dialog-body"
          className="mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto -mx-6 px-6 [mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)]"
        >
          <IncidentBanner
            tone="bad"
            title="Could not load engine presets"
            detail={errorMessage(error)}
            actions={
              <Button
                variant="outline"
                type="button"
                size="sm"
                onClick={() => void refetch()}
              >
                Retry
              </Button>
            }
          />
        </div>
      </DialogContent>
    );
  }
  if (!presets.length) {
    return (
      <DialogContent
        className="flex max-h-[85dvh] flex-col gap-0 overflow-hidden"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returnFocus.current?.focus();
        }}
      >
        <DialogHeader className="shrink-0 gap-1.5 pr-8 text-left">
          <DialogTitle>Add engine</DialogTitle>
          <DialogDescription>
            The gateway has not supplied an engine preset to select.
          </DialogDescription>
        </DialogHeader>
        <div
          data-slot="dialog-body"
          className="mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto -mx-6 px-6 [mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)]"
        >
          <EmptyOperationalState
            title="No engine presets available"
            detail="Add or restore a preset through the gateway configuration, then try again."
          />
        </div>
      </DialogContent>
    );
  }
  return (
    <DialogContent
      className="flex max-h-[85dvh] flex-col gap-0 overflow-hidden"
      onCloseAutoFocus={(event) => {
        event.preventDefault();
        returnFocus.current?.focus();
      }}
    >
      <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col">
        <DialogHeader className="shrink-0 gap-1.5 pr-8 text-left">
          <DialogTitle>Add engine</DialogTitle>
          <DialogDescription>
            Register an upstream engine. Select one API-supplied preset before
            entering its connection details. Changes apply live — no restart
            needed.
          </DialogDescription>
        </DialogHeader>
        <div
          data-slot="dialog-body"
          className="mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto -mx-6 px-6 [mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)]"
        >
          <div className="flex flex-col gap-4">
            {formError && (
              <p
                ref={errorSummaryRef}
                role="alert"
                tabIndex={-1}
                className="text-vf-small text-vf-bad"
              >
                {formError}
              </p>
            )}
            <Field
              label="Preset"
              required
              help="Choose an API-supplied preset to pre-fill its declared defaults."
              error={fieldErrors.preset}
            >
              <Select
                value={form.preset}
                onValueChange={(value) =>
                  setForm((current) => ({ ...current, preset: value }))
                }
                disabled={save.isPending}
                required
              >
                <SelectTrigger aria-label="Preset">
                  <SelectValue placeholder="Select a preset" />
                </SelectTrigger>
                <SelectContent>
                  {presets.map((preset) => (
                    <SelectItem key={preset.id} value={preset.id}>
                      {preset.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field label="Name" required error={fieldErrors.name}>
              <Input
                value={form.name}
                onChange={set("name")}
                placeholder="e.g. my-engine"
                disabled={save.isPending}
                autoFocus
              />
            </Field>
            <Field
              label="Base URL"
              required
              help="Full URL including scheme, host, port, and path."
              error={fieldErrors.base_url}
            >
              <Input
                value={form.base_url}
                onChange={set("base_url")}
                placeholder="http://127.0.0.1:8000/v1"
                disabled={save.isPending}
              />
            </Field>
            <Field
              label="API key env var"
              help="Name of an environment variable on the gateway host — never the key itself."
            >
              <Input
                value={form.api_key_env}
                onChange={set("api_key_env")}
                placeholder="e.g. SERVICE_API_KEY"
                disabled={save.isPending}
              />
            </Field>
            <Button
              type="button"
              size="sm"
              variant="outline"
              aria-busy={testing || undefined}
              disabled={testing || !form.base_url}
              onClick={() => void handleTest()}
            >
              {testing && <Spinner className="size-4" />}
              Test connection
            </Button>
            {probe && (
              <div className="rounded-vf-md flex items-center gap-2 border px-3 py-2 text-sm">
                <span>
                  {probe.ok
                    ? `Connected (HTTP ${probe.status ?? "ok"}) — ${probe.url}`
                    : probe.error || "Connection failed"}
                </span>
              </div>
            )}
          </div>
        </div>
        <DialogFooter className="mt-6 shrink-0">
          <Button
            size="md"
            type="button"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={save.isPending}
          >
            Cancel
          </Button>
          <Button
            disabled={save.isPending}
            size="md"
            type="submit"
            variant="default"
            aria-busy={save.isPending || undefined}
          >
            {save.isPending && <Spinner className="size-4" />}
            Add engine
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}
