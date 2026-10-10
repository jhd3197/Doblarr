import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { api, apiUrl, withKey } from '../../ui/src/lib/api.js';

const originalFetch = globalThis.fetch;
beforeEach(() => {
  const values = new Map();
  globalThis.localStorage = { getItem: k => values.get(k), setItem: (k, v) => values.set(k, v) };
  globalThis.window = { prompt: () => '', dispatchEvent: () => {} };
});
afterEach(() => { globalThis.fetch = originalFetch; });

test('API paths stay rooted when called from a nested settings or title page', () => {
  assert.equal(apiUrl('jobs'), '/api/jobs');
  assert.equal(apiUrl('api/plan?title=Film'), '/api/plan?title=Film');
});

test('JSON requests attach credentials and return parsed data', async () => {
  localStorage.setItem('doblarr_api_key', 'test-key');
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/jobs');
    assert.equal(options.headers.get('X-Api-Key'), 'test-key');
    assert.equal(options.headers.get('Content-Type'), 'application/json');
    assert.deepEqual(JSON.parse(options.body), { title: 'Film' });
    return Response.json({ ok: true });
  };
  assert.deepEqual(await api('jobs', { method: 'POST', json: { title: 'Film' } }), { ok: true });
});

test('server and validation errors reject rather than masquerading as success', async () => {
  globalThis.fetch = async () => Response.json({ error: 'Queue unavailable' }, { status: 503 });
  await assert.rejects(api('jobs'), /Queue unavailable/);
  globalThis.fetch = async () => Response.json({ detail: [{ loc: ['body', 'title'], msg: 'Required' }] }, { status: 422 });
  await assert.rejects(api('jobs'), /body.title: Required/);
});

test('invalid JSON produces a useful error', async () => {
  globalThis.fetch = async () => new Response('<html>oops</html>');
  await assert.rejects(api('jobs'), /invalid JSON/);
});

test('unauthorized requests retry with the supplied key', async () => {
  window.prompt = () => 'new-key';
  let calls = 0;
  globalThis.fetch = async (url, options) => {
    if (++calls === 1) return Response.json({ error: 'Unauthorized' }, { status: 401 });
    assert.equal(options.headers.get('X-Api-Key'), 'new-key');
    return Response.json({ ok: true });
  };
  await api('jobs');
  assert.equal(calls, 2);
  assert.equal(localStorage.getItem('doblarr_api_key'), 'new-key');
});

test('media URLs carry the key as a query parameter, joined correctly', () => {
  assert.equal(withKey('/api/events'), '/api/events');
  localStorage.setItem('doblarr_api_key', 'k&1');
  assert.equal(withKey('/api/events'), '/api/events?api_key=k%261');
  assert.equal(withKey('/api/clip?t=1'), '/api/clip?t=1&api_key=k%261');
});
