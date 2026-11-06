import { afterEach, expect, it, vi } from 'vitest';
import { productApi } from '@/lib/product-api';

afterEach(() => vi.unstubAllGlobals());

it('scrubs display payloads but retains exact IDs solely for matching', async () => {
  const raw = {
    aliases: [],
    models: [{
      id: '/private/fixture/models/Demo-Q4.gguf',
      title: 'Demo',
      resolves_to: { chat: '/private/fixture/models/Demo-Q4.gguf' },
      use: { model: '/private/fixture/models/Demo-Q4.gguf', path: '/v1/chat/completions' },
    }],
  };
  const fetchMock = vi.fn().mockImplementation(async () => Response.json(raw));
  vi.stubGlobal('fetch', fetchMock);
  const member = await productApi.models();
  const exact = await productApi.modelsByExactId();
  expect(member).toEqual({
    aliases: [],
    models: [{
      id: 'Demo-Q4.gguf', title: 'Demo',
      resolves_to: { chat: 'Demo-Q4.gguf' },
      use: { model: 'Demo-Q4.gguf', path: '/v1/chat/completions' },
    }],
  });
  expect(JSON.stringify(member)).not.toContain('/private/fixture');
  expect(exact).toEqual(raw);
  expect(raw.models[0].id).toBe('/private/fixture/models/Demo-Q4.gguf');
  expect(fetchMock).toHaveBeenCalledTimes(2);
  for (const [path, init] of fetchMock.mock.calls) {
    expect(path).toBe('/me/api/models/catalog');
    expect(init).toMatchObject({ credentials: 'same-origin' });
  }
});

it('preserves structured member failure and handles invalid JSON failure safely', async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(Response.json({
      error: { code: 'authentication_required', message: 'Sign in' },
    }, { status: 401 }))
    .mockResolvedValueOnce(new Response('<html>failure</html>', { status: 503 }));
  vi.stubGlobal('fetch', fetchMock);
  await expect(productApi.status()).rejects.toMatchObject({
    code: 'authentication_required', status: 401, message: 'Sign in',
  });
  await expect(productApi.home()).rejects.toMatchObject({
    code: 'unexpected_response', status: 503,
    message: 'The gateway could not complete this request.',
  });
  expect(fetchMock).toHaveBeenCalledTimes(2);
});
