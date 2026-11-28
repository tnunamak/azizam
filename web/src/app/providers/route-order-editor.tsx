"use client";
import { useState } from "react";
import { adminFetch, type RouteEditorState, type RouteTargetChoice } from "@/lib/api";
import { Button } from "@/components/ui/button";
const key = (row: RouteTargetChoice) => `${row.service_id}\n${row.model_id}`;
const endpoint = "/admin/api/operator/model-routes";
export function RouteOrderEditor({ serviceId, modelId }: { serviceId: string; modelId: string }) {
  const [saved, setSaved] = useState<RouteEditorState | null>(null);
  const [draft, setDraft] = useState<RouteTargetChoice[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const dirty = !!saved && JSON.stringify(saved.routes.map(key)) !== JSON.stringify(draft.map(key));
  async function read() {
    const result = await adminFetch<{ route_editor: RouteEditorState }>(`${endpoint}?${new URLSearchParams({ service_id: serviceId, model_id: modelId })}`);
    setSaved(result.route_editor);
    return result.route_editor;
  }
  async function load() {
    setOpen(true); setBusy(true); setError("");
    try { setDraft((await read()).routes); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Could not load routes."); }
    finally { setBusy(false); }
  }
  function move(index: number, direction: number) {
    const next = [...draft];
    [next[index], next[index + direction]] = [next[index + direction], next[index]];
    setDraft(next); setNotice("");
  }
  async function save() {
    if (!saved) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await adminFetch<{ route_editor: RouteEditorState }>(endpoint, { method: "PUT",
        headers: { "If-Match": `"os-${saved.generation}"` }, body: { service_id: serviceId, model_id: modelId,
          targets: draft.map(({ service_id, model_id }) => ({ service_id, model_id })) } });
      setSaved(result.route_editor); setDraft(result.route_editor.routes); setNotice("Routes saved");
    } catch (failure) {
      setError(`${failure instanceof Error ? failure.message : "Could not save routes."} Your draft is kept. Compare it with the current saved order before retrying.`);
      try { await read(); } catch { /* The original error remains actionable. */ }
    } finally { setBusy(false); }
  }
  if (!open) return <Button size="sm" variant="outline" onClick={() => void load()}>Route order for {modelId}</Button>;
  return <div role="group" aria-label={`Route order for ${modelId}`} className="space-y-3">
    <p>Choose routes that serve this same model. A paid route placed first is used directly; provider grants and spend limits still apply. Pins select one route and never fall back.</p>
    {error && <p role="alert">{error}</p>}
    {saved && <>
      <p>Current saved order: {saved.routes.map((route) => route.name).join(" → ")}</p>
      <ol className="space-y-2">{draft.map((route, index) => <li key={key(route)} className="flex flex-wrap items-center gap-2">
        <span>{index + 1}. {route.name} · {route.upstream_model} · {route.billing_class} · {route.available ? "Configured" : "Unavailable"}</span>
        <Button size="sm" variant="outline" disabled={busy || index === 0} aria-label={`Move ${route.name} up`} onClick={() => move(index, -1)}>Move up</Button>
        <Button size="sm" variant="outline" disabled={busy || index === draft.length - 1} aria-label={`Move ${route.name} down`} onClick={() => move(index, 1)}>Move down</Button>
        <Button size="sm" variant="outline" disabled={busy || draft.length === 1} aria-label={`Remove ${route.name}`} onClick={() => { setDraft(draft.filter((_, at) => at !== index)); setNotice(""); }}>Remove</Button>
      </li>)}</ol>
      <label className="block">Add route for {modelId}
        <select className="border-input rounded-control min-h-11 border bg-background px-3 py-2" value="" disabled={busy}
          onChange={(event) => { const row = saved.targets.find((target) => key(target) === event.target.value); if (row) { setDraft([...draft, row]); setNotice(""); } }}>
          <option value="">Choose a same-model target</option>
          {saved.targets.filter((target) => !draft.some((row) => key(row) === key(target))).map((target) =>
            <option key={key(target)} value={key(target)} disabled={!target.available}>{target.name} · {target.upstream_model} · {target.billing_class}</option>)}
        </select>
      </label>
      <p>Paid routes may charge your API key. Your access plans still decide who can use them. Paid fallback requires both the person&apos;s and app&apos;s grants.</p>
      {dirty && <p>Unsaved route order</p>}
      <Button disabled={busy || !dirty} onClick={() => void save()}>Save routes</Button>
    </>}
    <Button variant="outline" disabled={busy} onClick={() => { setOpen(false); setError(""); setNotice(""); }}>Cancel</Button>
    <p role="status">{busy ? "Loading…" : notice}</p>
  </div>;
}
