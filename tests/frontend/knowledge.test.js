import assert from 'node:assert/strict';
import { test } from 'node:test';
import { entriesQuery, pageCount } from '../../web/js/knowledge.js';
import { baseOf, scopeOptions } from '../../web/js/knowledge-correction.js';

test('entriesQuery encodes filters and pagination', () => {
  assert.equal(entriesQuery({}), 'knowledge/entries?page=1&page_size=25');
  assert.equal(
    entriesQuery({ locale: 'es-MX', kind: 'term', status: 'proposed', q: 'ginko', page: 3 }),
    'knowledge/entries?locale=es-MX&kind=term&status=proposed&q=ginko&page=3&page_size=25',
  );
  assert.equal(
    entriesQuery({ scope: 'show', pageSize: 50 }),
    'knowledge/entries?scope=show&page=1&page_size=50',
  );
});

test('pageCount always shows at least one page', () => {
  assert.equal(pageCount(0, 25), 1);
  assert.equal(pageCount(25, 25), 1);
  assert.equal(pageCount(26, 25), 2);
});

test('scope options follow the refs available in context', () => {
  const all = scopeOptions({ lineRef: 't#3', titleRef: 't', showRef: 'series:1' });
  assert.deepEqual(all.map(o => o.value), ['line', 'episode', 'show', 'personal']);
  assert.equal(all[0].ref, 't#3');
  const personal = scopeOptions({});
  assert.deepEqual(personal.map(o => o.value), ['personal']);
  assert.equal(baseOf('es-MX'), 'es');
  assert.equal(baseOf('es-419'), 'es');
});
