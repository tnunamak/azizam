"use client";

import { FormEvent, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type OperationTarget } from "@/lib/api";
import { Button, EmptyState, SectionHeader } from "@/components/ui";

function etag(target: OperationTarget) {
  return `"ot-${target.target_generation}-os-${target.operator_settings_generation}"`;
}

function randomKey() {
  return crypto.randomUUID().replaceAll("-", "");
}

export default function OperationTargetsPage() {
  const cache = useQueryClient();
  const targets = useQuery({ queryKey: ["operation-targets"], queryFn: api.operationTargets.list });
  const registry = useQuery({ queryKey: ["service-registry"], queryFn: api.services.registry });
  const [serviceId, setServiceId] = useState("");
  const [aliasId, setAliasId] = useState("");
  const [baseModelId, setBaseModelId] = useState("");
  const [overlay, setOverlay] = useState("{}");
  const [evidence, setEvidence] = useState("{\n  \"expires_at\": \"2030-01-01T00:00:00Z\"\n}");
  const [message, setMessage] = useState("");
  const [history, setHistory] = useState<Record<string, unknown> | null>(null);
  const selected = useMemo(() => targets.data?.find((item) => item.service_id === serviceId && item.alias_id === aliasId), [targets.data, serviceId, aliasId]);
  const revisions = useQuery({ queryKey: ["operation-revisions", serviceId, aliasId], queryFn: () => api.operationTargets.revisions(serviceId, aliasId), enabled: Boolean(serviceId && aliasId) });
  const services = Object.values(registry.data?.services ?? {});
  const models = services.find((service) => service.id === serviceId)?.config.models ?? [];
  const aliases = models.filter((model) => model.base_model_id);
  const bases = models.filter((model) => !model.base_model_id);
  const refresh = () => cache.invalidateQueries({ queryKey: ["operation-targets"] });
  async function importPackage(file: File | undefined) {
    if (!file) return;
    try {
      const value = JSON.parse(await file.text()) as { format?: string; base_model_id?: string; overlay?: unknown; evidence?: unknown };
      if (value.format !== "gateway-operation-activation-package/v1" || !value.base_model_id || !value.overlay || !value.evidence) throw new Error("not a complete activation package");
      setBaseModelId(value.base_model_id); setOverlay(JSON.stringify(value.overlay, null, 2)); setEvidence(JSON.stringify(value.evidence, null, 2)); setMessage("Activation package loaded. Server validation is still required before staging.");
    } catch { setMessage("Choose a complete activation package produced by operation_evidence.py."); }
  }

  async function stage(event: FormEvent) {
    event.preventDefault();
    try {
      await api.operationTargets.stage(serviceId, aliasId, { base_model_id: baseModelId, overlay: JSON.parse(overlay), evidence: JSON.parse(evidence) });
      setMessage("Candidate staged outside the active registry."); refresh();
    } catch (error) { setMessage(error instanceof ApiError ? error.message : "Could not stage candidate."); }
  }
  async function validate(target = selected) {
    if (!target) return;
    try { const result = await api.operationTargets.validate(target.service_id, target.alias_id, {}); setMessage(`Validation succeeded: ${JSON.stringify(result)}`); }
    catch (error) { setMessage(error instanceof ApiError ? error.message : "Validation failed."); }
  }
  async function activate(target = selected) {
    if (!target) return;
    try { await api.operationTargets.activate(target.service_id, target.alias_id, etag(target), randomKey()); setMessage("Activation preparation requested. Refreshing status…"); refresh(); }
    catch (error) { setMessage(error instanceof ApiError ? error.message : "Activation failed."); }
  }
  async function deactivate(target = selected) {
    if (!target) return;
    try { await api.operationTargets.deactivate(target.service_id, target.alias_id, etag(target), randomKey()); setMessage("Target deactivated. Refreshing status…"); refresh(); }
    catch (error) { setMessage(error instanceof ApiError ? error.message : "Deactivation failed."); }
  }
  async function rollback(target = selected) {
    if (!target || target.effective_revision === null) return;
    try { await api.operationTargets.rollback(target.service_id, target.alias_id, target.effective_revision, etag(target)); setMessage("Rollback candidate staged for preparation. Refreshing status…"); refresh(); }
    catch (error) { setMessage(error instanceof ApiError ? error.message : "Rollback failed."); }
  }
  async function showRevision(target: OperationTarget, number: number | null) {
    if (number === null) return;
    try { const result = await api.operationTargets.revision(target.service_id, target.alias_id, number); setHistory(result as Record<string, unknown>); }
    catch (error) { setMessage(error instanceof ApiError ? error.message : "Could not load revision history."); }
  }

  return <main className="space-y-6 p-6">
    <SectionHeader title="Operation targets" description="Stage → validate → activate. Candidates remain outside active discovery and execution until durable cutover." />
    <form onSubmit={stage} className="grid gap-3 rounded-lg border p-4 md:grid-cols-3">
      <label className="md:col-span-3 text-sm">Activation package <input className="mt-1 block" type="file" accept="application/json" onChange={(event) => importPackage(event.target.files?.[0])} /><span className="text-muted-foreground">Generated locally by the evidence CLI; the browser reads this selected file only.</span></label>
      <label className="text-sm">Service<select className="mt-1 w-full rounded border p-2" value={serviceId} onChange={(event) => { setServiceId(event.target.value); setAliasId(""); }} required><option value="">Choose service</option>{services.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}</select></label>
      <label className="text-sm">Alias<select className="mt-1 w-full rounded border p-2" value={aliasId} onChange={(event) => { const alias = aliases.find((item) => item.id === event.target.value); setAliasId(event.target.value); setBaseModelId(alias?.base_model_id ?? ""); }} required><option value="">Choose alias</option>{aliases.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}</select></label>
      <label className="text-sm">Base model<select className="mt-1 w-full rounded border p-2" value={baseModelId} onChange={(event) => setBaseModelId(event.target.value)} required><option value="">Choose base</option>{bases.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}</select></label>
      <p className="md:col-span-3 text-sm text-muted-foreground">The selectors are the normal path. This operation requires an operator-declared compaction-safety contract and evidence cross-bindings; the gateway cannot invent those validated assertions. Supply them below as declarative data only.</p>
      <label className="md:col-span-3 text-sm">Advanced overlay JSON <textarea className="mt-1 min-h-24 w-full rounded border p-2 font-mono" value={overlay} onChange={(event) => setOverlay(event.target.value)} /></label>
      <label className="md:col-span-3 text-sm">Advanced evidence assertion JSON <textarea className="mt-1 min-h-24 w-full rounded border p-2 font-mono" value={evidence} onChange={(event) => setEvidence(event.target.value)} /></label>
      <Button type="submit">Stage candidate</Button>
      <Button type="button" variant="secondary" onClick={() => validate()} disabled={!selected}>Validate</Button>
      <Button type="button" variant="secondary" onClick={() => activate()} disabled={!selected}>Activate</Button>
    </form>
    {message ? <p role="status" className="rounded border p-3 text-sm">{message}</p> : null}
    {history ? <pre className="overflow-auto rounded border p-3 text-xs">{JSON.stringify(history, null, 2)}</pre> : null}
    {revisions.data?.length ? <section className="rounded-lg border p-4"><h2 className="font-medium">Immutable history</h2><div className="mt-3 space-y-2">{revisions.data.map((revision) => <div key={revision.revision} className="flex items-center justify-between text-sm"><span>Revision {revision.revision} · {revision.requested_state}</span><div className="flex gap-2"><Button type="button" variant="secondary" onClick={() => selected && showRevision(selected, revision.revision)}>View</Button><Button type="button" variant="secondary" disabled={!selected} onClick={() => selected && api.operationTargets.rollback(selected.service_id, selected.alias_id, revision.revision, etag(selected)).then(() => { setMessage("Rollback candidate staged."); refresh(); }).catch((error) => setMessage(error instanceof ApiError ? error.message : "Rollback failed."))}>Rollback</Button></div></div>)}</div></section> : null}
    {targets.data?.length === 0 ? <EmptyState>Use the form above to stage a data-declared target.</EmptyState> : null}
    <div className="space-y-3">{targets.data?.map((target) => <article key={`${target.service_id}/${target.alias_id}`} className="rounded-lg border p-4">
      <h2 className="font-medium">{target.service_id} / {target.alias_id}</h2>
      <p className="text-sm text-muted-foreground">{target.candidate_state}; effective revision {target.effective_revision ?? "none"}; local reload {target.last_local_reload_ack_generation === target.operator_settings_generation ? "acknowledged" : "not yet acknowledged"}</p>
      {target.last_error_code ? <p className="text-sm text-destructive">Preparation error: {target.last_error_code}</p> : null}
      <div className="mt-3 flex gap-2"><Button type="button" variant="secondary" onClick={() => { setServiceId(target.service_id); setAliasId(target.alias_id); }}>Inspect</Button><Button type="button" variant="secondary" onClick={() => validate(target)}>Validate</Button><Button type="button" variant="secondary" onClick={() => activate(target)}>Activate</Button><Button type="button" variant="secondary" onClick={() => deactivate(target)}>Deactivate</Button><Button type="button" variant="secondary" onClick={() => showRevision(target, target.effective_revision)}>History</Button><Button type="button" variant="secondary" onClick={() => rollback(target)} disabled={target.effective_revision === null}>Rollback revision {target.effective_revision ?? "—"}</Button></div>
    </article>)}</div>
  </main>;
}
