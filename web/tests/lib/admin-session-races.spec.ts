import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { fetchSession, resetSessionCache } from '@/lib/api';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const session = (subject: string) => ({
  actor: { subject, email: null, roles: [], csrf_token: 'fixture-' + subject },
});
beforeEach(resetSessionCache);
afterEach(() => {
  resetSessionCache();
  vi.unstubAllGlobals();
});

it('shares concurrent reads and reuses the completed session', async () => {
  const pending = deferred<Response>();
  const fetchMock = vi.fn().mockReturnValue(pending.promise);
  vi.stubGlobal('fetch', fetchMock);
  const first = fetchSession();
  const second = fetchSession();
  expect(fetchMock).toHaveBeenCalledTimes(1);
  pending.resolve(Response.json(session('current')));
  await expect(first).resolves.toEqual(session('current'));
  await expect(second).resolves.toEqual(session('current'));
  await expect(fetchSession()).resolves.toEqual(session('current'));
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
