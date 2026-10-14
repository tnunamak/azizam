"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, ApiError, queryKeys, type ServiceRegistry } from "@/lib/api";
import { useViewer } from "@/components/shell/session";
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
import { EmptyState } from "@/components/product/empty-state";
import { Callout } from "@/components/product/status-ui";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
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
  endpoint: string;
  ok: boolean;
  status: number | null;
  error: string;
  url: string;
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 412) {
      return "Engine settings changed elsewhere. Refresh and try again.";
    }
    // The exact reason stays under Technical details.
    if (error.status === 400 || error.status === 409) {
      return "The gateway did not accept these engine settings. Check the fields and save again.";
    }
  }
  return "Engine settings could not be saved. We could not identify the cause. Try saving again.";
}

function emptyFormState(): ServiceFormState {
  return { name: "", preset: "", base_url: "", api_key_env: "" };
}

function formToApiBody(
  state: ServiceFormState,
  canAttachKey: boolean,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: state.name.trim(),
    preset: state.preset,
    base_url: state.base_url.trim(),
  };
  if (canAttachKey && state.api_key_env.trim())
    body.api_key_env = state.api_key_env.trim();
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
  // Attaching a key to a service sends the owner's secret to its host.
  const viewer = useViewer();
  const canAttachKey = !(viewer.admin && viewer.role !== "owner");
  const [form, setForm] = useState<ServiceFormState>(emptyFormState);
  const [formError, setFormError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<
    Partial<Record<"name" | "base_url" | "preset", string>>
  >({});
  const [probe, setProbe] = useState<TestProbe | null>(null);
  const [testing, setTesting] = useState(false);
  const probeVersion = useRef(0);
  const [technicalError, setTechnicalError] = useState<string | null>(null);

  useEffect(() => () => { probeVersion.current += 1; }, []);
  const errorSummaryRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (formError) errorSummaryRef.current?.focus();
  }, [formError]);

  const save = useMutation({
    mutationFn: () => api.services.add(formToApiBody(form, canAttachKey)),
    onSuccess: onSaved,
    onError: (err) => {
      setFormError(errorMessage(err));
      setTechnicalError(err instanceof Error ? err.message : null);
    },
  });

  function set(key: keyof ServiceFormState) {
    return (event: React.ChangeEvent<HTMLInputElement>) => {
      if (key === "base_url") {
        probeVersion.current += 1;
        setProbe(null);
        setTesting(false);
      }
      setForm((current) => ({ ...current, [key]: event.target.value }));
    };
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    setTechnicalError(null);
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
    const endpoint = form.base_url.trim();
    const version = ++probeVersion.current;
    setTesting(true);
    setProbe(null);
    try {
      const result = await api.services.testConnection({ base_url: endpoint });
      if (version === probeVersion.current) setProbe({ ...result, endpoint });
    } catch (err) {
      if (version === probeVersion.current) {
        setProbe({
          endpoint, ok: false, status: null, url: endpoint,
          error: err instanceof Error ? err.message : "",
        });
      }
    } finally {
      if (version === probeVersion.current) setTesting(false);
    }
  }

  if (loading) {
    return (
      <DialogContent
        className="flex! max-h-[85dvh] flex-col gap-0! overflow-hidden"
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
          className="-mx-6 mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto [mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)] px-6"
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
        className="flex! max-h-[85dvh] flex-col gap-0! overflow-hidden"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returnFocus.current?.focus();
        }}
      >
        <DialogHeader className="shrink-0 gap-1.5 pr-8 text-left">
          <DialogTitle>Add engine</DialogTitle>
          <DialogDescription>
            Engine presets are supplied by the gateway.
          </DialogDescription>
        </DialogHeader>
        <div
          data-slot="dialog-body"
          className="-mx-6 mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto [mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)] px-6"
        >
          <Callout
            as="div"
            severity="critical"
            title="Could not load engine presets"
            meta="Try loading the engine presets again."
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
          {error instanceof Error && (
            <details>
              <summary>Technical details</summary>
              <p className="break-words">{error.message}</p>
            </details>
          )}
        </div>
      </DialogContent>
    );
  }
  if (!presets.length) {
    return (
      <DialogContent
        className="flex! max-h-[85dvh] flex-col gap-0! overflow-hidden"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returnFocus.current?.focus();
        }}
      >
        <DialogHeader className="shrink-0 gap-1.5 pr-8 text-left">
          <DialogTitle>Add engine</DialogTitle>
          <DialogDescription>
            Engine presets are supplied by the gateway.
          </DialogDescription>
        </DialogHeader>
        <div
          data-slot="dialog-body"
          className="-mx-6 mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto [mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)] px-6"
        >
          <EmptyState
            title="No engine presets available"
            description="Add or restore a preset through the gateway configuration, then try again."
          />
        </div>
      </DialogContent>
    );
  }
  return (
    <DialogContent
      className="flex! max-h-[85dvh] flex-col gap-0! overflow-hidden"
      onCloseAutoFocus={(event) => {
        event.preventDefault();
        returnFocus.current?.focus();
      }}
    >
      <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col">
        <DialogHeader className="shrink-0 gap-1.5 pr-8 text-left">
          <DialogTitle>Add engine</DialogTitle>
        </DialogHeader>
        <div
          data-slot="dialog-body"
          className="-mx-6 mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto [mask-image:linear-gradient(to_bottom,transparent_0,black_8px,black_calc(100%-8px),transparent_100%)] px-6"
        >
          <DialogDescription>
            Choose an engine preset, then enter its connection details.
            Changes take effect without restarting the gateway.
          </DialogDescription>
          <div className="flex flex-col gap-4">
            {formError && (
              <p
                ref={errorSummaryRef}
                role="alert"
                tabIndex={-1}
                className="text-danger text-[length:var(--text-sm)]"
              >
                {formError}
              </p>
            )}
            {technicalError && (
              <details>
                <summary>Technical details</summary>
                <p className="break-words">{technicalError}</p>
              </details>
            )}
            <Field
              label="Preset"
              required
              help="Choose the preset that matches your engine."
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
                <SelectTrigger className="w-full min-w-0" aria-label="Preset">
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
              label="Key environment variable"
              help={
                canAttachKey
                  ? "Enter the gateway's environment variable name, not the key."
                  : "Only the owner can change this."
              }
            >
              <Input
                value={canAttachKey ? form.api_key_env : ""}
                onChange={set("api_key_env")}
                placeholder="e.g. SERVICE_API_KEY"
                disabled={save.isPending || !canAttachKey}
              />
            </Field>
            <Button
              type="button"
              size="sm"
              variant="outline"
              aria-busy={testing || undefined}
              disabled={testing || !form.base_url.trim()}
              onClick={() => void handleTest()}
            >
              {testing && <Spinner className="size-4" />}
              {testing ? "Checking this address…" : "Test connection"}
            </Button>
            {probe && probe.endpoint === form.base_url.trim() && (
              <div role="status" className="rounded-[var(--radius)] border px-3 py-2 text-sm">
                <p>
                  {probe.ok
                    ? "Connected to this address."
                    : "The connection check failed. Check the address and try again."}
                </p>
                <details>
                  <summary>Technical details</summary>
                  <p className="break-words">{probe.url}</p>
                  {probe.status !== null && <p>HTTP {probe.status}</p>}
                  {probe.error && <p className="break-words">{probe.error}</p>}
                </details>
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
