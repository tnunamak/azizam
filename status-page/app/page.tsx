'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

type BackendRole = 'required' | 'optional' | 'standby';

type BackendStatus = {
  name: string;
  url: string;
  status: 'up' | 'down' | 'degraded';
  live_status?: 'up' | 'down' | 'degraded';
  role?: BackendRole;
  note?: string | null;
  latency_ms: number | null;
  last_error?: string | null;
};

type GpuWorkload = {
  label: string;
  detail: string | null;
  running?: number;
  pending?: number;
  active_slots?: number;
  total_slots?: number;
};

type GpuInfo = {
  index: number;
  memory_used_mb: number;
  memory_total_mb: number;
  // Renamed from `utilization_pct`. Kept available for power users; not
  // shown on the page because instantaneous nvidia-smi util is misleading
  // for memory-bandwidth-bound workloads (LLM inference, diffusion).
  instantaneous_util_pct?: number;
  active_workload?: GpuWorkload | null;
};

type Gpu0Budget = {
  total_gb: number | null;
  desktop_gb: number | null;
  tts_gb: number | null;
  comfyui_gb: number | null;
  headroom_gb: number | null;
};

type Gpu1Budget = {
  total_gb: number | null;
  llm_gb: number | null;
  headroom_gb: number | null;
};

type ImagesBudget = {
  max_batch_size: number | null;
  vram_reserve_bytes: number | null;
};

type VramEstimateEntry = {
  key: string;
  peak_bytes: number;
};

type VramEstimates = {
  last_modified: number;
  entries: VramEstimateEntry[];
  total_entries: number;
};

type Budgets = {
  gpu0: Gpu0Budget | null;
  gpu1: Gpu1Budget | null;
  images: ImagesBudget;
  vram_estimates_recent: VramEstimates | null;
};

type VideoCap = {
  alias: string;
  workflow: string;
  max_seconds: number;
  max_size: string;
  frame_rate: number;
  supports_i2v: boolean;
  supports_audio: boolean;
};

type RecentRender = {
  filename: string;
  size_bytes: number;
  mtime: number;
  alias: string;
};

type Status = {
  generated_at: number;
  cache_age_s: number;
  banner: 'ok' | 'warn' | 'bad';
  backends: BackendStatus[];
  gpus: GpuInfo[] | null;
  comfy_queue: { running: number; pending: number } | null;
  models: {
    loaded_llm_path: string | null;
    loaded_llm_name: string | null;
    mmproj_path: string | null;
    vision_capable: boolean;
    ctx_size: number | null;
    ctx_used: number | null;
    active_tts_engine: string | null;
    image_workflows: string[];
    video_aliases: string[];
  } | null;
  capabilities: VideoCap[];
  recent_renders: RecentRender[];
  driver: {
    last_xid_109: string[];
    uptime_since: string | null;
    disk_avail_models: string | null;
  } | null;
  budgets: Budgets | null;
  errors: Record<string, string>;
};

const STORAGE_KEY = 'openai-vivid-fish.api-key';
const REFRESH_MS = 8000;

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function fmtAgo(epochSec: number): string {
  const diff = Math.max(0, Date.now() / 1000 - epochSec);
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

function basename(p: string | null | undefined): string {
  if (!p) return '';
  const i = p.lastIndexOf('/');
  return i >= 0 ? p.slice(i + 1) : p;
}

export default function Page() {
  const [apiKey, setApiKey] = useState<string>('');
  const [keyDraft, setKeyDraft] = useState<string>('');
  const [status, setStatus] = useState<Status | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [authNeeded, setAuthNeeded] = useState<boolean>(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [lastFetchAt, setLastFetchAt] = useState<number>(0);
  const [sortKey, setSortKey] = useState<keyof VideoCap>('alias');
  const [sortDesc, setSortDesc] = useState<boolean>(false);
  const timerRef = useRef<number | null>(null);

  // Hydrate API key from localStorage on mount.
  useEffect(() => {
    try {
      const k = window.localStorage.getItem(STORAGE_KEY) || '';
      if (k) setApiKey(k);
    } catch {}
  }, []);

  const fetchStatus = useCallback(async () => {
    setFetchError(null);
    try {
      const headers: Record<string, string> = { Accept: 'application/json' };
      if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`;
      const r = await fetch('/v1/status', { headers, cache: 'no-store' });
      if (r.status === 401) {
        setAuthNeeded(true);
        setLoading(false);
        return;
      }
      if (!r.ok) {
        setFetchError(`HTTP ${r.status}`);
        setLoading(false);
        return;
      }
      const j = (await r.json()) as Status;
      setStatus(j);
      setAuthNeeded(false);
      setLastFetchAt(Date.now());
    } catch (e: any) {
      setFetchError(e?.message || 'fetch failed');
    } finally {
      setLoading(false);
    }
  }, [apiKey]);

  useEffect(() => {
    fetchStatus();
    if (timerRef.current) window.clearInterval(timerRef.current);
    timerRef.current = window.setInterval(fetchStatus, REFRESH_MS);
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current);
    };
  }, [fetchStatus]);

  const onSaveKey = () => {
    const k = keyDraft.trim();
    try {
      window.localStorage.setItem(STORAGE_KEY, k);
    } catch {}
    setApiKey(k);
    setKeyDraft('');
  };

  const onClearKey = () => {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {}
    setApiKey('');
  };

  const sortedCaps = useMemo(() => {
    if (!status) return [];
    const copy = [...status.capabilities];
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      let cmp = 0;
      if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv;
      else if (typeof av === 'boolean' && typeof bv === 'boolean') cmp = Number(av) - Number(bv);
      else cmp = String(av).localeCompare(String(bv));
      return sortDesc ? -cmp : cmp;
    });
    return copy;
  }, [status, sortKey, sortDesc]);

  if (authNeeded) {
    return (
      <div className="wrap">
        <header className="banner warn">
          <div>
            <h1>API key required</h1>
            <div className="sub">
              This dashboard polls <span className="mono">/v1/status</span>, which needs a proxy bearer token
              when accessed from a non-loopback address.
            </div>
          </div>
        </header>
        <section className="card">
          <div className="name">Paste an opk_ key</div>
          <div className="meta small">Stored locally in your browser only.</div>
          <input
            className="key-input"
            type="password"
            placeholder="opk_..."
            value={keyDraft}
            onChange={(e) => setKeyDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') onSaveKey();
            }}
            autoFocus
          />
          <div className="flex-row">
            <button className="primary" onClick={onSaveKey} disabled={!keyDraft.trim()}>
              Save and continue
            </button>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="wrap">
      <Banner status={status} loading={loading} fetchError={fetchError} />

      {status && (
        <>
          <BackendsSection backends={status.backends} />
          <GpusSection gpus={status.gpus} comfy={status.comfy_queue} />
          <BudgetsSection budgets={status.budgets} />
          <ModelsSection models={status.models} />
          <CapabilitiesSection
            caps={sortedCaps}
            sortKey={sortKey}
            sortDesc={sortDesc}
            onSort={(k) => {
              if (k === sortKey) setSortDesc(!sortDesc);
              else {
                setSortKey(k);
                setSortDesc(false);
              }
            }}
          />
          <RendersSection renders={status.recent_renders} />
          <DriverSection driver={status.driver} />
        </>
      )}

      <footer>
        <div>
          <a href="/health">/health</a>
          <span className="muted"> · </span>
          <a href="/v1/models">/v1/models</a>
          {apiKey && (
            <>
              <span className="muted"> · </span>
              <button
                className="ghost"
                style={{ marginTop: 0, padding: '2px 8px', fontSize: 11 }}
                onClick={onClearKey}
              >
                clear key
              </button>
            </>
          )}
        </div>
        <div>
          {lastFetchAt > 0 && <>updated {fmtAgo(lastFetchAt / 1000)} · refreshes every {REFRESH_MS / 1000}s</>}
        </div>
      </footer>
    </div>
  );
}

function Banner({
  status,
  loading,
  fetchError,
}: {
  status: Status | null;
  loading: boolean;
  fetchError: string | null;
}) {
  let cls = 'banner';
  let dotCls = 'dot pulse';
  let title = 'Loading...';
  let sub = '';

  if (fetchError) {
    cls += ' bad';
    dotCls += ' bad';
    title = 'Cannot reach proxy';
    sub = fetchError;
  } else if (loading && !status) {
    sub = 'Polling /v1/status...';
  } else if (status) {
    if (status.banner === 'ok') {
      cls += ' ok';
      dotCls += ' ok';
      title = 'All systems operational';
    } else if (status.banner === 'warn') {
      cls += ' warn';
      dotCls += ' warn';
      title = 'Degraded';
    } else {
      cls += ' bad';
      dotCls += ' bad';
      title = 'Outage';
    }
    // Exclude `standby` backends from down/degraded counts — idle is expected.
    const counted = status.backends.filter((b) => (b.role || 'required') !== 'standby');
    const downCount = counted.filter((b) => (b.live_status || b.status) === 'down').length;
    const degCount = counted.filter((b) => (b.live_status || b.status) === 'degraded').length;
    const standbyCount = status.backends.length - counted.length;
    const standbyNote = standbyCount > 0 ? ` · ${standbyCount} standby` : '';
    sub = `${status.backends.length} backends · ${downCount} down · ${degCount} degraded${standbyNote}`;
  }

  return (
    <header className={cls}>
      <div>
        <h1>
          <span className={dotCls} />
          {title}
        </h1>
        <div className="sub">{sub}</div>
      </div>
      <div className="right mono small">openai.vivid.fish</div>
    </header>
  );
}

const ROLE_ORDER: Record<BackendRole, number> = { required: 0, optional: 1, standby: 2 };

function badgeFor(role: BackendRole, live: 'up' | 'down' | 'degraded'): {
  dotCls: string;
  cardCls: string;
  tag: { text: string; cls: string } | null;
  label: string;
} {
  // standby: grey when idle, green-with-tag when live; never red/yellow.
  if (role === 'standby') {
    if (live === 'up') {
      return {
        dotCls: 'dot ok',
        cardCls: 'card',
        tag: { text: 'STANDBY · LIVE', cls: 'tag ok' },
        label: 'standby (live)',
      };
    }
    if (live === 'degraded') {
      return {
        dotCls: 'dot standby',
        cardCls: 'card standby',
        tag: { text: 'STANDBY', cls: 'tag' },
        label: 'standby (degraded)',
      };
    }
    return {
      dotCls: 'dot standby',
      cardCls: 'card standby',
      tag: { text: 'STANDBY', cls: 'tag' },
      label: 'standby (idle)',
    };
  }
  if (role === 'optional') {
    if (live === 'up') return { dotCls: 'dot ok', cardCls: 'card', tag: null, label: 'up' };
    if (live === 'degraded')
      return { dotCls: 'dot warn', cardCls: 'card', tag: null, label: 'degraded' };
    return { dotCls: 'dot warn', cardCls: 'card', tag: null, label: 'down (optional)' };
  }
  // required
  if (live === 'up') return { dotCls: 'dot ok', cardCls: 'card', tag: null, label: 'up' };
  if (live === 'degraded')
    return { dotCls: 'dot warn', cardCls: 'card', tag: null, label: 'degraded' };
  return { dotCls: 'dot bad', cardCls: 'card', tag: null, label: 'down' };
}

function BackendsSection({ backends }: { backends: BackendStatus[] }) {
  // Sort required → optional → standby; preserve original order within each group.
  const sorted = useMemo(() => {
    const indexed = backends.map((b, i) => ({ b, i }));
    indexed.sort((a, z) => {
      const ra = ROLE_ORDER[(a.b.role || 'required') as BackendRole] ?? 0;
      const rz = ROLE_ORDER[(z.b.role || 'required') as BackendRole] ?? 0;
      if (ra !== rz) return ra - rz;
      return a.i - z.i;
    });
    return indexed.map((x) => x.b);
  }, [backends]);

  return (
    <section>
      <h2>Backends</h2>
      <div className="grid cols-3">
        {sorted.map((b) => {
          const role = (b.role || 'required') as BackendRole;
          const live = (b.live_status || b.status) as 'up' | 'down' | 'degraded';
          const { dotCls, cardCls, tag, label } = badgeFor(role, live);
          return (
            <div className={cardCls} key={b.name} title={b.note || undefined}>
              <div className="name">
                <span className={dotCls} />
                {b.name}
                {tag && (
                  <>
                    {' '}
                    <span className={tag.cls}>{tag.text}</span>
                  </>
                )}
              </div>
              <div className="meta mono">{b.url}</div>
              <div className="meta">
                {label}
                {b.latency_ms != null ? ` · ${b.latency_ms} ms` : ''}
              </div>
              {b.note && <div className="meta small muted">{b.note}</div>}
              {b.last_error && <div className="err mono">{b.last_error}</div>}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function workloadTagCls(label: string | undefined): string {
  switch (label) {
    case 'running':
    case 'processing':
      return 'tag ok';
    case 'loaded':
      return 'tag';
    case 'idle':
      return 'tag';
    case 'unknown':
    default:
      return 'tag';
  }
}

function GpusSection({
  gpus,
  comfy,
}: {
  gpus: GpuInfo[] | null;
  comfy: { running: number; pending: number } | null;
}) {
  return (
    <section>
      <h2>GPUs &amp; queue</h2>
      <div className="grid cols-2">
        {gpus?.map((g) => {
          const used = g.memory_used_mb;
          const total = g.memory_total_mb;
          const pct = total > 0 ? (used / total) * 100 : 0;
          const cls = pct > 90 ? 'bad' : pct > 70 ? 'warn' : 'ok';
          // We deliberately do NOT render `instantaneous_util_pct`: a single
          // nvidia-smi sample is meaningless for memory-bandwidth-bound LLM
          // and diffusion workloads. The number is still in the JSON for
          // power users who want it.
          const wl = g.active_workload;
          return (
            <div className="card" key={g.index}>
              <div className="name">GPU{g.index}</div>
              <div className="meta mono">
                {used.toLocaleString()} / {total.toLocaleString()} MiB ({pct.toFixed(1)}%)
              </div>
              <div className={`bar ${cls}`}>
                <span style={{ width: `${Math.min(100, pct)}%` }} />
              </div>
              {wl && (
                <div className="meta">
                  <span className={workloadTagCls(wl.label)}>{wl.label}</span>
                  {wl.detail && <span className="mono small muted"> · {wl.detail}</span>}
                </div>
              )}
            </div>
          );
        })}
        {!gpus && (
          <div className="card">
            <div className="name muted">GPU info unavailable</div>
          </div>
        )}
        <div className="card">
          <div className="name">ComfyUI queue</div>
          <div className="meta">
            {comfy ? (
              <>
                <span className="tag ok">{comfy.running} running</span>{' '}
                <span className="tag">{comfy.pending} pending</span>
              </>
            ) : (
              <span className="muted">unavailable</span>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

function fmtGiB(bytes: number | null | undefined): string {
  if (!bytes && bytes !== 0) return '—';
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GiB`;
}

function BudgetsSection({ budgets }: { budgets: Budgets | null }) {
  if (!budgets) {
    return (
      <section>
        <h2>Budgets</h2>
        <div className="card">
          <div className="muted">unavailable</div>
        </div>
      </section>
    );
  }

  const g0 = budgets.gpu0;
  const g1 = budgets.gpu1;
  const img = budgets.images;
  const vr = budgets.vram_estimates_recent;

  // GPU0 line: "desktop 3 + TTS 4 + ComfyUI 15 + headroom 2 = 24"
  const g0Sum =
    g0 != null
      ? [g0.desktop_gb, g0.tts_gb, g0.comfyui_gb, g0.headroom_gb]
          .map((v) => (v == null ? 0 : v))
          .reduce((a, b) => a + b, 0)
      : null;
  const g1Sum =
    g1 != null
      ? [g1.llm_gb, g1.headroom_gb]
          .map((v) => (v == null ? 0 : v))
          .reduce((a, b) => a + b, 0)
      : null;

  return (
    <section>
      <h2>Budgets</h2>
      <div className="card">
        <div className="name">GPU budgets <span className="mono small muted">(from gpu_budget.sh)</span></div>
        <div className="meta mono small">
          {g0
            ? `GPU0 (${g0.total_gb ?? '—'} GB):  desktop ${g0.desktop_gb ?? '—'} + TTS ${g0.tts_gb ?? '—'} + ComfyUI ${g0.comfyui_gb ?? '—'} + headroom ${g0.headroom_gb ?? '—'} = ${g0Sum ?? '—'}`
            : 'GPU0: unavailable'}
        </div>
        <div className="meta mono small">
          {g1
            ? `GPU1 (${g1.total_gb ?? '—'} GB):  LLM ${g1.llm_gb ?? '—'} + headroom ${g1.headroom_gb ?? '—'} = ${g1Sum ?? '—'}`
            : 'GPU1: unavailable'}
        </div>
      </div>

      <div className="card">
        <div className="name">Image generation</div>
        <div className="meta mono small">
          max_batch: {img.max_batch_size ?? '—'} · VRAM reserve: {fmtGiB(img.vram_reserve_bytes)}
        </div>
        {vr && vr.entries.length > 0 ? (
          <>
            <div className="meta small muted">
              Recent per-image cost ({vr.total_entries} measurement{vr.total_entries === 1 ? '' : 's'}; cache updated {fmtAgo(vr.last_modified)}):
            </div>
            {vr.entries.map((e) => (
              <div className="meta mono small" key={e.key}>
                {e.key} → {fmtGiB(e.peak_bytes)}
              </div>
            ))}
          </>
        ) : (
          <div className="meta small muted">No VRAM measurements cached yet.</div>
        )}
      </div>
    </section>
  );
}

function ModelsSection({ models }: { models: Status['models'] }) {
  if (!models) {
    return (
      <section>
        <h2>Models</h2>
        <div className="card">
          <div className="muted">unavailable</div>
        </div>
      </section>
    );
  }
  return (
    <section>
      <h2>Models</h2>
      <div className="card">
        <div className="kv">
          <div className="k">LLM</div>
          <div className="v">{models.loaded_llm_name || basename(models.loaded_llm_path) || '—'}</div>
          <div className="k">Path</div>
          <div className="v">{models.loaded_llm_path || '—'}</div>
          <div className="k">Vision</div>
          <div className="v">
            {models.vision_capable ? (
              <span className="tag ok">enabled</span>
            ) : (
              <span className="tag">text-only</span>
            )}
            {models.mmproj_path && <span className="muted small"> · mmproj: {basename(models.mmproj_path)}</span>}
          </div>
          <div className="k">Context</div>
          <div className="v">
            {models.ctx_used != null && models.ctx_size
              ? `${models.ctx_used.toLocaleString()} / ${models.ctx_size.toLocaleString()} tokens`
              : models.ctx_size
                ? `${models.ctx_size.toLocaleString()} tokens`
                : '—'}
          </div>
          <div className="k">TTS</div>
          <div className="v">{models.active_tts_engine || '—'}</div>
          <div className="k">Image WFs</div>
          <div className="v">{models.image_workflows.join(', ') || '—'}</div>
          <div className="k">Video aliases</div>
          <div className="v">{models.video_aliases.join(', ') || '—'}</div>
        </div>
      </div>
    </section>
  );
}

function CapabilitiesSection({
  caps,
  sortKey,
  sortDesc,
  onSort,
}: {
  caps: VideoCap[];
  sortKey: keyof VideoCap;
  sortDesc: boolean;
  onSort: (k: keyof VideoCap) => void;
}) {
  const arrow = (k: keyof VideoCap) => (k === sortKey ? (sortDesc ? ' ↓' : ' ↑') : '');
  return (
    <section>
      <h2>Video capabilities</h2>
      <table>
        <thead>
          <tr>
            <th onClick={() => onSort('alias')}>Alias{arrow('alias')}</th>
            <th onClick={() => onSort('workflow')}>Workflow{arrow('workflow')}</th>
            <th onClick={() => onSort('max_seconds')}>Max sec{arrow('max_seconds')}</th>
            <th onClick={() => onSort('max_size')}>Max size{arrow('max_size')}</th>
            <th onClick={() => onSort('frame_rate')}>FPS{arrow('frame_rate')}</th>
            <th onClick={() => onSort('supports_i2v')}>I2V{arrow('supports_i2v')}</th>
            <th onClick={() => onSort('supports_audio')}>Audio{arrow('supports_audio')}</th>
          </tr>
        </thead>
        <tbody>
          {caps.map((c) => (
            <tr key={c.alias}>
              <td className="mono">{c.alias}</td>
              <td className="mono">{c.workflow}</td>
              <td>{c.max_seconds}</td>
              <td className="mono">{c.max_size}</td>
              <td>{c.frame_rate}</td>
              <td>{c.supports_i2v ? <span className="tag ok">yes</span> : <span className="tag">no</span>}</td>
              <td>{c.supports_audio ? <span className="tag ok">yes</span> : <span className="tag">no</span>}</td>
            </tr>
          ))}
          {caps.length === 0 && (
            <tr>
              <td colSpan={7} className="muted">
                no aliases configured
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

function RendersSection({ renders }: { renders: RecentRender[] }) {
  return (
    <section>
      <h2>Recent renders</h2>
      <table>
        <thead>
          <tr>
            <th>Filename</th>
            <th>Alias</th>
            <th>Size</th>
            <th>Age</th>
          </tr>
        </thead>
        <tbody>
          {renders.map((r) => (
            <tr key={r.filename}>
              <td className="mono">{r.filename}</td>
              <td className="mono">{r.alias || '—'}</td>
              <td>{fmtBytes(r.size_bytes)}</td>
              <td>{fmtAgo(r.mtime)}</td>
            </tr>
          ))}
          {renders.length === 0 && (
            <tr>
              <td colSpan={4} className="muted">
                no renders yet
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}

function DriverSection({ driver }: { driver: Status['driver'] }) {
  return (
    <section>
      <h2>Driver &amp; system</h2>
      <div className="card">
        <div className="kv">
          <div className="k">Last Xid 109</div>
          <div className="v">
            {driver?.last_xid_109 && driver.last_xid_109.length > 0
              ? driver.last_xid_109.join(' · ')
              : 'none in 24h'}
          </div>
          <div className="k">Uptime since</div>
          <div className="v">{driver?.uptime_since || '—'}</div>
          <div className="k">Models disk free</div>
          <div className="v">{driver?.disk_avail_models || '—'}</div>
        </div>
      </div>
    </section>
  );
}
