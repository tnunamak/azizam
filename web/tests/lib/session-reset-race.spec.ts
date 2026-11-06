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

it('does not revive an invalidated session when its response arrives last', async () => {
  const old = deferred<Response>();
  const fetchMock = vi.fn()
    .mockReturnValueOnce(old.promise)
    .mockResolvedValueOnce(Response.json(session('new')));
  vi.stubGlobal('fetch', fetchMock);
  const abandoned = fetchSession();
  resetSessionCache();
  await expect(fetchSession()).resolves.toEqual(session('new'));
  old.resolve(Response.json(session('old')));
  await abandoned;
  await expect(fetchSession()).resolves.toEqual(session('new'));
  expect(fetchMock).toHaveBeenCalledTimes(2);
});
