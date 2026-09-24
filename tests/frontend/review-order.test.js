import assert from 'node:assert/strict';
import test from 'node:test';

import { defaultOrder, orderRows } from '../../web/js/review-order.js';

const rows = [
  { index: 0, start: 1 },
  { index: 1, start: 5, review_priority: { score: 3, reasons: ['timing_overflow'] } },
  { index: 2, start: 9, review_priority: { score: 5, reasons: ['silence'] } },
  { index: 3, start: 12, review_priority: { score: 3, reasons: ['text_mismatch'] } },
];

test('likeliest problems first, ties keep their place on the timeline', () => {
  assert.deepEqual(orderRows(rows, 'likely').map(r => r.index), [2, 1, 3, 0]);
  assert.deepEqual(orderRows(rows, 'timeline').map(r => r.index), [0, 1, 2, 3]);
  assert.deepEqual(rows.map(r => r.index), [0, 1, 2, 3]);  // input untouched
});

test('a review opens likeliest-first only when the run scored its lines', () => {
  assert.equal(defaultOrder({ settings: { review_order: true }, segments: rows }), 'likely');
  assert.equal(defaultOrder({ settings: { review_order: false }, segments: rows }), 'timeline');
  assert.equal(defaultOrder({ settings: { review_order: true }, segments: [{ index: 0 }] }), 'timeline');
  assert.equal(defaultOrder(null), 'timeline');
});
