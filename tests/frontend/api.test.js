import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { api, apiUrl } from '../../web/js/api.js';
import { getJobs } from '../../web/js/jobs-data.js';

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

test('concurrent job reads share a request and recover after failure', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json({ error: 'offline' }, { status: 503 }); };
  await Promise.allSettled([getJobs(), getJobs(), getJobs()]);
  assert.equal(calls, 1);
  globalThis.fetch = async () => { calls++; return Response.json({ jobs: [] }); };
  assert.deepEqual(await getJobs(), { jobs: [] });
  assert.equal(calls, 2);
});
