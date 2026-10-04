"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  api,
  adminRegistryMutation,
  type Service,
  type ServiceRegistry,
  type ServiceRegistryResponse,
} from "@/lib/api";
import { Button, Field, IncidentBanner, Input } from "@/components/ui";
import { detectedModelsPatch, type DetectedModelRow } from "./detected-models";

type CheckResult = {
  ok: boolean;
  title: string;
  detail: string;
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
          ? `Connected to ${probe.url} (${probe.status ?? "ok"}). This checks reachability; send a request to verify a model.`
          : `${probe.url || endpoint}${probe.status ? ` (HTTP ${probe.status})` : ""}: ${probe.error || "Connection failed."} Check that the engine is running and this address is reachable from the gateway.`,
      }),
    onError: (error) =>
      setResult({
        ok: false,
        title: `${service.name}: connection failed`,
        retry: "test",
        detail: `${endpoint}: ${message(error)} Check the endpoint and try again.`,
      }),
  });
  const detect = useMutation({
    mutationFn: () => api.services.detectModels(target),
    onMutate: () => {
      setResult(null);
      setRows(null);
      setApplyError(null);
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
          detail: `${endpoint}: ${detected.error || "The engine returned no model names. Load a model, then detect again, or configure served models in Advanced fields."}`,
        });
      }
    },
    onError: (error) =>
      setResult({
        ok: false,
        title: `${service.name}: detection failed`,
        retry: "detect",
        detail: `${endpoint}: ${message(error)} Check the endpoint or configure served models in Advanced fields.`,
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
          "Public model names are saved. Existing access plans still control who can use them. Send a request with an allowed public name to verify routing.",
      });
    },
    onError: (error) => setApplyError(message(error)),
  });
  const busy = test.isPending || detect.isPending || apply.isPending;

  return (
    <div className="mt-5 flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="secondary"
          loading={test.isPending}
          disabled={busy || undefined}
          onClick={() => {
            setResult(null);
            test.mutate();
          }}
        >
          Test
        </Button>
        <Button
          size="sm"
          variant="secondary"
          loading={detect.isPending}
          disabled={busy || undefined}
          onClick={() => detect.mutate()}
        >
          Detect
        </Button>
      </div>
      {result && (
        <IncidentBanner
          tone={result.ok ? "info" : "bad"}
          title={result.title}
          detail={result.detail}
          className="[&_.vf-incident-banner-body]:break-words"
          actions={
            !result.ok ? (
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  onClick={() =>
                    result.retry === "detect" ? detect.mutate() : test.mutate()
                  }
                  disabled={busy}
                >
                  {result.retry === "detect" ? "Retry detection" : "Retry test"}
                </Button>
                <Button size="sm" variant="ghost" onClick={onEdit}>
                  Edit endpoint
                </Button>
              </div>
            ) : undefined
          }
        />
      )}
      {rows && (
        <section
          aria-label={`Detected models for ${service.name}`}
          className="min-w-0 border-t border-[var(--vf-border)] pt-4"
        >
          <h3 className="text-vf-body font-semibold">
            Preview detected models
          </h3>
          <p className="text-vf-small text-vf-muted mt-1 break-all">
            Detected models: {rows.map((row) => row.upstream).join(", ")}
          </p>
          <p className="text-vf-small text-vf-muted mt-1">
            Choose the names clients will send. Existing mappings and request
            policies are kept.
          </p>
          <div className="mt-4 grid gap-4">
            {rows.map((row, index) => (
              <div
                key={row.upstream}
                className="grid min-w-0 gap-2 sm:grid-cols-2 sm:items-center"
              >
                <label className="flex min-h-11 min-w-0 items-center gap-3 text-sm">
                  <input
                    type="checkbox"
                    checked={row.selected}
                    disabled={apply.isPending}
                    onChange={(event) => {
                      setApplyError(null);
                      setRows(
                        rows.map((item, i) =>
                          i === index
                            ? { ...item, selected: event.target.checked }
                            : item,
                        ),
                      );
                    }}
                  />
                  <span className="min-w-0 break-all">{row.upstream}</span>
                </label>
                <Field label={`Public name for ${row.upstream}`}>
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
              </div>
            ))}
          </div>
          <div className="mt-4">
            <p className="text-vf-small text-vf-muted">
              Access plans with unrestricted or wildcard model access may
              include these names immediately. Restricted plans need an existing
              matching grant. No access plan is edited here.
            </p>
            <label className="mt-2 flex min-h-11 items-center gap-3 text-sm">
              <input
                type="checkbox"
                checked={accessConfirmed}
                disabled={apply.isPending}
                onChange={(event) => setAccessConfirmed(event.target.checked)}
              />
              Use existing access rules for these models
            </label>
            <a
              href="/ui/people/?tab=plans"
              className="text-vf-small text-vf-muted inline-flex min-h-11 items-center underline underline-offset-4"
            >
              Review access plans in People
            </a>
          </div>
          {applyError && (
            <p
              role="alert"
              className="text-vf-small text-vf-bad mt-3 break-words"
            >
              {applyError}
            </p>
          )}
          {service.editable === false ? (
            <p className="text-vf-small text-vf-muted mt-3">
              This engine is managed by the gateway. Model mappings cannot be
              edited here.
            </p>
          ) : (
            <div className="mt-4 flex flex-wrap gap-2">
              <Button
                variant="primary"
                loading={apply.isPending}
                disabled={!accessConfirmed || !rows.some((row) => row.selected)}
                onClick={() => {
                  setApplyError(null);
                  apply.mutate();
                }}
              >
                Apply models
              </Button>
              <Button
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
