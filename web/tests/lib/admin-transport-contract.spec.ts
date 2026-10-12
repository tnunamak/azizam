import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { adminFetch, ApiError, createQueryClient, resetSessionCache } from '@/lib/api';
import { networkFetch } from '@/lib/network-error';

const session = (token: string) => Response.json({
  actor: { subject: 'fixture-user', email: null, roles: [], csrf_token: token },
});
const csrfDenied = () => Response.json({
  error: { code: 'admin_csrf_required', message: 'Refresh session' },
}, { status: 403 });
beforeEach(resetSessionCache);
afterEach(() => {
  resetSessionCache();
  vi.unstubAllGlobals();
});

it('replays a rolled-session write once with the same payload and signal', async () => {
  const signal = new AbortController().signal;
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(session('old'))
    .mockResolvedValueOnce(csrfDenied())
    .mockResolvedValueOnce(session('new'))
    .mockResolvedValueOnce(new Response(null, { status: 204 }));
  vi.stubGlobal('fetch', fetchMock);
  await expect(adminFetch('/admin/api/test-write', {
    method: 'POST', body: { value: 7 }, signal,
  })).resolves.toBeUndefined();
  expect(fetchMock).toHaveBeenCalledTimes(4);
  for (const index of [1, 3]) {
    const [path, init] = fetchMock.mock.calls[index];
    expect(path).toBe('/admin/api/test-write');
    expect(init).toMatchObject({
      method: 'POST', credentials: 'same-origin',
      body: JSON.stringify({ value: 7 }), signal,
    });
    expect(new Headers(init.headers).get('x-azizam-admin-action')).toBe('1');
    expect(new Headers(init.headers).get('content-type')).toBe('application/json');
  }
  expect(new Headers(fetchMock.mock.calls[1][1].headers).get('x-azizam-csrf')).toBe('old');
  expect(new Headers(fetchMock.mock.calls[3][1].headers).get('x-azizam-csrf')).toBe('new');
});

it('stops after a second CSRF refusal', async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(session('old'))
    .mockResolvedValueOnce(csrfDenied())
    .mockResolvedValueOnce(session('new'))
    .mockResolvedValueOnce(csrfDenied());
  vi.stubGlobal('fetch', fetchMock);
  await expect(adminFetch('/admin/api/test-write', { method: 'POST' }))
    .rejects.toMatchObject({ status: 403, code: 'admin_csrf_required' });
  expect(fetchMock).toHaveBeenCalledTimes(4);
});

it('does not replay another 403 or bootstrap a session for a GET', async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(session('one'))
    .mockResolvedValueOnce(Response.json({
      error: { code: 'permission_denied', message: 'No access' },
    }, { status: 403 }))
    .mockResolvedValueOnce(new Response('not-json', { status: 502 }));
  vi.stubGlobal('fetch', fetchMock);
  await expect(adminFetch('/admin/api/test-write', { method: 'POST' }))
    .rejects.toMatchObject({ code: 'permission_denied', status: 403 });
  expect(fetchMock).toHaveBeenCalledTimes(2);
  await expect(adminFetch('/admin/api/test-read'))
    .rejects.toMatchObject({ code: 'unexpected_response', status: 502 });
  expect(fetchMock).toHaveBeenCalledTimes(3);
  expect(new Headers(fetchMock.mock.calls[2][1].headers).has('x-azizam-csrf')).toBe(false);
});

it('preserves intentional cancellation identity', async () => {
  const signal = new AbortController().signal;
  const aborted = new DOMException('Stopped', 'AbortError');
  const fetchMock = vi.fn().mockRejectedValue(aborted);
  vi.stubGlobal('fetch', fetchMock);
  await expect(networkFetch('/fixture', { signal })).rejects.toBe(aborted);
  expect(fetchMock).toHaveBeenCalledWith('/fixture', { signal });
});

it('bounds transient retries and refuses deterministic or mutation retries', () => {
  const client = createQueryClient();
  try {
    const retry = client.getDefaultOptions().queries?.retry;
    expect(typeof retry).toBe('function');
    if (typeof retry !== 'function') throw new Error('Expected retry predicate');
    expect(retry(0, new ApiError('denied', 'Denied', 403))).toBe(false);
    expect(retry(0, new ApiError('conflict', 'Conflict', 412))).toBe(false);
    expect(retry(0, new ApiError('unavailable', 'Unavailable', 503))).toBe(true);
    expect(retry(1, new TypeError('Failed to fetch'))).toBe(true);
    expect(retry(2, new TypeError('Failed to fetch'))).toBe(false);
    expect(client.getDefaultOptions().mutations?.retry).toBe(false);
  } finally {
    client.clear();
  }
});
