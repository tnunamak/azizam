import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const registryPath = '/admin/api/operator/service-registry';
const mutationPath = '/admin/api/operator/services/demo';
const snapshot = (etag: string) => Response.json({
  service_registry: { schema_version: 1, services: {} },
}, { headers: { etag } });
const session = () => Response.json({
  actor: { subject: 'fixture', email: null, roles: [], csrf_token: 'fixture' },
});
const conflict = () => Response.json({
  error: { code: 'generation_mismatch', message: 'Fixture conflict' },
}, { status: 412 });

// This file imports only the transport module dynamically, not a rendered React tree.
beforeEach(() => vi.resetModules());
afterEach(() => vi.unstubAllGlobals());

it('keeps the conflict visible, refreshes its ETag, and waits for explicit retry', async () => {
  const { adminFetch, adminRegistryMutation } = await import('@/lib/api');
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(snapshot('"one"'))
    .mockResolvedValueOnce(session())
    .mockResolvedValueOnce(conflict())
    .mockResolvedValueOnce(snapshot('"two"'))
    .mockResolvedValueOnce(snapshot('"three"'));
  vi.stubGlobal('fetch', fetchMock);
  await adminFetch(registryPath);
  await expect(adminRegistryMutation(mutationPath, {
    method: 'PATCH', body: { name: 'Draft' },
  })).rejects.toMatchObject({ code: 'generation_mismatch', status: 412 });
  expect(fetchMock).toHaveBeenCalledTimes(4);
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'PATCH')).toHaveLength(1);
  expect(new Headers(fetchMock.mock.calls[2][1].headers).get('if-match')).toBe('"one"');
  await adminRegistryMutation(mutationPath, { method: 'PATCH', body: { name: 'Draft' } });
  expect(new Headers(fetchMock.mock.calls[4][1].headers).get('if-match')).toBe('"two"');
});

it('preserves an explicit case-insensitive precondition', async () => {
  const { adminFetch } = await import('@/lib/api');
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(snapshot('"cached"'))
    .mockResolvedValueOnce(session())
    .mockResolvedValueOnce(snapshot('"result"'));
  vi.stubGlobal('fetch', fetchMock);
  await adminFetch(registryPath);
  await adminFetch(mutationPath, {
    method: 'PATCH', headers: { 'if-match': '"caller"' }, body: {},
  });
  expect(new Headers(fetchMock.mock.calls[2][1].headers).get('if-match')).toBe('"caller"');
});

it('preserves the original conflict when the refresh also fails', async () => {
  const { adminFetch, adminRegistryMutation } = await import('@/lib/api');
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(snapshot('"one"'))
    .mockResolvedValueOnce(session())
    .mockResolvedValueOnce(conflict())
    .mockRejectedValueOnce(new TypeError('Failed to fetch'));
  vi.stubGlobal('fetch', fetchMock);
  await adminFetch(registryPath);
  await expect(adminRegistryMutation(mutationPath, { method: 'DELETE' }))
    .rejects.toMatchObject({
      code: 'generation_mismatch', status: 412, message: 'Fixture conflict',
    });
  expect(fetchMock).toHaveBeenCalledTimes(4);
});

it('refuses an unguarded registry write before any network request', async () => {
  const { adminFetch } = await import('@/lib/api');
  const fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  await expect(adminFetch(mutationPath, { method: 'PATCH', body: {} }))
    .rejects.toMatchObject({ code: 'generation_mismatch', status: 412 });
  expect(fetchMock).not.toHaveBeenCalled();
});
