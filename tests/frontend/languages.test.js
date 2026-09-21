import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { languageName, loadLanguages, targetChoices } from '../../web/js/languages.js';

const originalFetch = globalThis.fetch;
beforeEach(() => {
  const values = new Map();
  globalThis.localStorage = { getItem: k => values.get(k), setItem: (k, v) => values.set(k, v) };
  globalThis.window = { prompt: () => '', dispatchEvent: () => {} };
});
afterEach(() => { globalThis.fetch = originalFetch; });

const CATALOG = [
  { id: 'en', name: 'English', base: 'en', script: null, region: null, supported: true },
  { id: 'es', name: 'Spanish', base: 'es', script: null, region: null, supported: true },
  { id: 'es-MX', name: 'Spanish — Mexico', base: 'es', script: null, region: 'MX', supported: true },
  { id: 'es-VE', name: 'Spanish — Venezuela', base: 'es', script: null, region: 'VE', supported: true },
];

test('display names fall back to the uppercased code before the catalog loads', () => {
  assert.equal(languageName('es-MX'), 'ES-MX');
  assert.deepEqual(targetChoices(['en', 'es'], 'es'), ['en', 'es']);
});

test('the catalog drives display names and regional target choices', async () => {
  globalThis.fetch = async url => {
    assert.equal(url, '/api/languages');
    return Response.json({ languages: CATALOG });
  };
  await loadLanguages();
  assert.equal(languageName('es-MX'), 'Spanish — Mexico');
  assert.equal(languageName('fr-CA'), 'FR-CA');
  assert.deepEqual(targetChoices(['en', 'es'], 'es'), ['en', 'es', 'es-MX', 'es-VE']);
  assert.deepEqual(targetChoices(['en'], 'en'), ['en']);
});
