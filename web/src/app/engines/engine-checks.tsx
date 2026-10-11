"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  api,
  ApiError,
  adminRegistryMutation,
  type Service,
  type ServiceRegistry,
  type ServiceRegistryResponse,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Field } from "@/components/ui/field";
import { Callout } from "@/components/product/status-ui";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { detectedModelsPatch, type DetectedModelRow } from "./detected-models";

type CheckResult = {
  ok: boolean;
  title: string;
  detail: string;
  technical?: string;
  retry?: "test" | "detect";
};

function message(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "The check failed. Try again.";
}

export async function applyDetectedModels(
  serviceId: string,
  rows: DetectedModelRow[],
) {
  // Pin the model merge and write to one registry snapshot, even when another
  // browser request updates the shared API client's latest ETag.
  const response = await fetch("/admin/api/operator/service-registry", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok)
    throw new Error(
      `Registry could not be refreshed (HTTP ${response.status}). Try again.`,
    );
  const etag = response.headers.get("etag");
  if (!etag)
    throw new Error("Registry version is unavailable. Refresh and try again.");
  const { service_registry: registry } =
    (await response.json()) as ServiceRegistryResponse;
  const current = registry.services[serviceId];
  if (!current)
    throw new Error("This engine was removed. Refresh the engine list.");
  if (current.editable === false)
    throw new Error(
      "This engine is managed by the gateway. Model mappings cannot be edited here.",
    );
  const saved = await adminRegistryMutation<ServiceRegistryResponse>(
    `/admin/api/operator/services/${encodeURIComponent(serviceId)}`,
    {
      method: "PATCH",
      headers: { "If-Match": etag },
      body: detectedModelsPatch(current.config.models, rows),
    },
  );
  return saved.service_registry;
}

export function EngineChecks({
  service,
  endpoint,
  onSaved,
  onEdit,
}: {
  service: Service;
  endpoint: string;
  onSaved: (registry: ServiceRegistry) => void;
  onEdit: () => void;
}) {
  const [result, setResult] = useState<CheckResult | null>(null);
  const [rows, setRows] = useState<DetectedModelRow[] | null>(null);
  const [accessConfirmed, setAccessConfirmed] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applyTechnical, setApplyTechnical] = useState<string | null>(null);
  const target = {
    base_url: service.config.base_url,
    host: service.config.host,
    port: service.config.port,
    base_path: service.config.base_path,
  };
  const test = useMutation({
    mutationFn: () =>
      api.services.testConnection({
        ...target,
        health_endpoint: service.config.health_endpoint,
      }),
    onSuccess: (probe) =>
      setResult({
        ok: probe.ok,
        retry: "test",
        title: `${service.name}: ${probe.ok ? "connection passed" : "connection failed"}`,
        detail: probe.ok
          ? "This checks reachability; send a request to verify a model."
          : "Check that the engine is running and this address is reachable from the gateway.",
        technical: `${probe.url || endpoint}${probe.status !== null ? ` (HTTP ${probe.status})` : ""}${probe.error ? `: ${probe.error}` : ""}`,
      }),
    onError: (error) =>
      setResult({
        ok: false,
        title: `${service.name}: connection failed`,
        retry: "test",
        detail: "The connection check could not finish. Check the endpoint and try again.",
        technical: `${endpoint}: ${message(error)}`,
      }),
  });
  const detect = useMutation({
    mutationFn: () => api.services.detectModels(target),
    onMutate: () => {
      setResult(null);
      setRows(null);
      setApplyError(null);
      setApplyTechnical(null);
      setAccessConfirmed(false);
    },
    onSuccess: (detected) => {
      if (detected.ok && detected.models.length) {
        setRows(
          [...new Set(detected.models)].map((upstream) => ({
            upstream,
            publicName: upstream,
            selected: true,
          })),
        );
        setResult(null);
      } else {
        setResult({
          ok: detected.ok,
          retry: "detect",
          title: `${service.name}: ${detected.ok ? "no models detected" : "detection failed"}`,
          detail: detected.ok
            ? "The engine returned no model names. Load a model, then detect again."
            : "Model detection failed. Check the endpoint and try again.",
          technical: detected.error ? `${endpoint}: ${detected.error}` : undefined,
        });
      }
    },
    onError: (error) =>
      setResult({
        ok: false,
        title: `${service.name}: detection failed`,
        retry: "detect",
        detail: "Model detection could not finish. Check the endpoint and try again.",
        technical: `${endpoint}: ${message(error)}`,
      }),
  });
  const apply = useMutation({
    mutationFn: () => {
      if (!accessConfirmed)
        throw new Error("Choose how access plans apply before saving.");
      return applyDetectedModels(service.id, rows ?? []);
    },
    onSuccess: (registry) => {
      onSaved(registry);
      setRows(null);
      setResult({
        ok: true,
        title: `${service.name}: models applied`,
        detail:
          "Model names saved. Existing access plans control who can use them.",
      });
    },
    onError: (error) => {
      setApplyError(error instanceof ApiError && error.status === 412
        ? "Engine settings changed elsewhere. Refresh and try again."
        : "Model names could not be saved. Review the names and try again.");
      setApplyTechnical(message(error));
    },
  });
  const busy = test.isPending || detect.isPending || apply.isPending;

  return (
    <div className="mt-5 flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-busy={test.isPending || undefined}
          disabled={busy || undefined}
          onClick={() => {
            setResult(null);
            test.mutate();
          }}
        >
          {test.isPending && <Spinner className="size-4" />}
          Test
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-busy={detect.isPending || undefined}
          disabled={busy || undefined}
          onClick={() => detect.mutate()}
        >
          {detect.isPending && <Spinner className="size-4" />}
          Detect
        </Button>
      </div>
      {result && (
        <div role={result.ok ? "status" : undefined}>
          <Callout
            as="div"
            severity={result.ok ? "info" : "critical"}
            title={result.title}
            meta={result.detail}
            className="break-words"
            actions={
              !result.ok ? (
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="outline"
                    type="button"
                    size="sm"
                    onClick={() =>
                      result.retry === "detect"
                        ? detect.mutate()
                        : test.mutate()
                    }
                    disabled={busy}
                  >
                    {result.retry === "detect"
                      ? "Retry detection"
                      : "Retry test"}
                  </Button>
                  {service.editable !== false && (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={onEdit}
                    >
                      Edit endpoint
                    </Button>
                  )}
                </div>
              ) : undefined
            }
          />
          {result.technical && (
            <details>
              <summary>Technical details</summary>
              <p className="text-meta break-words">{result.technical}</p>
            </details>
          )}
        </div>
      )}
      {rows && (
        <section
          aria-label={`Detected models for ${service.name}`}
          className="border-border min-w-0 border-t pt-4"
        >
          <h3 className="text-body font-semibold">Preview detected models</h3>
          <p className="text-meta text-muted-foreground mt-1">
            Choose the model names to use in requests. Existing settings are kept.
          </p>
          <div className="mt-4 grid gap-4">
            {rows.map((row, index) => (
              <div
                key={row.upstream}
                role="group"
                aria-label={`Model ${index + 1}`}
                className="grid min-w-0 gap-2 sm:grid-cols-2 sm:items-center"
              >
                <label className="text-meta flex min-h-8 min-w-0 items-center gap-3 max-sm:min-h-11">
                  <Checkbox
                    className="max-sm:min-h-11 max-sm:min-w-11"
                    aria-label={`Include model ${index + 1}`}
                    checked={row.selected}
                    disabled={apply.isPending}
                    onCheckedChange={(checked) => {
                      setApplyError(null);
                      setRows(
                        rows.map((item, i) =>
                          i === index
                            ? { ...item, selected: checked === true }
                            : item,
                        ),
                      );
                    }}
                  />
                  Include model
                </label>
                <Field label="Model name">
                  <Input
                    value={row.publicName}
                    disabled={!row.selected || apply.isPending}
                    onChange={(event) => {
                      setApplyError(null);
                      setRows(
                        rows.map((item, i) =>
                          i === index
                            ? { ...item, publicName: event.target.value }
                            : item,
                        ),
                      );
                    }}
                  />
                </Field>
                <details className="sm:col-span-2">
                  <summary>Technical details</summary>
                  <p className="text-meta break-all">Engine model name: {row.upstream}</p>
                </details>
              </div>
            ))}
          </div>
          <div className="mt-4">
            <p className="text-meta text-muted-foreground">
              Access plans that allow all models may use these models immediately.
              Other access plans must already allow their names.
            </p>
            <label className="text-meta mt-2 flex min-h-8 items-center gap-3 max-sm:min-h-11">
              <Checkbox
                className="max-sm:min-h-11 max-sm:min-w-11"
                aria-label="Use existing access rules for these models"
                checked={accessConfirmed}
                disabled={apply.isPending}
                onCheckedChange={(checked) =>
                  setAccessConfirmed(checked === true)
                }
              />
              Use existing access rules for these models
            </label>
            <a
              href="/ui/people/?tab=plans"
              className="text-meta text-muted-foreground inline-flex min-h-8 items-center underline underline-offset-4 max-sm:min-h-11"
            >
              Review access plans in People
            </a>
          </div>
          {applyError && (
            <p role="alert" className="text-meta text-danger mt-3 break-words">
              {applyError}
            </p>
          )}
          {applyError && applyTechnical && (
            <details>
              <summary>Technical details</summary>
              <p className="text-meta break-words">{applyTechnical}</p>
            </details>
          )}
          {service.editable === false ? (
            <p className="text-meta text-muted-foreground mt-3">
              This engine is managed by the gateway. Model mappings cannot be
              edited here.
            </p>
          ) : (
            <div className="mt-4 flex flex-wrap gap-2">
              <Button
                size="md"
                type="button"
                variant="default"
                aria-busy={apply.isPending || undefined}
                disabled={
                  apply.isPending ||
                  !accessConfirmed ||
                  !rows.some((row) => row.selected)
                }
                onClick={() => {
                  setApplyError(null);
                  apply.mutate();
                }}
              >
                {apply.isPending && <Spinner className="size-4" />}
                Apply models
              </Button>
              <Button
                size="md"
                type="button"
                variant="ghost"
                disabled={apply.isPending}
                onClick={() => setRows(null)}
              >
                Cancel preview
              </Button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
