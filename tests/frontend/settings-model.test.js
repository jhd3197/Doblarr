import assert from 'node:assert/strict';
import test from 'node:test';

import { FIELD_BY_KEY, PLAN_FIELDS, TABS } from '../../web/js/settings-model.js';
import { SOURCES } from '../../web/js/scene-player.js';

// Settings describe presentation only, so what these check is that every
// control a person can reach corresponds to a real config key and says what it
// does. A dial with no explanation is how a feature gets turned on by someone
// who could not have known what it would change.

const groupTitles = TABS.flatMap(tab => tab.groups.map(g => g.title));

test('the new audio sections are reachable from the settings page', () => {
  assert.ok(groupTitles.includes('Scene space and devices'));
  assert.ok(groupTitles.includes('Export checks'));
});

test('every treatment and delivery control exists and is explained', () => {
  const keys = [
    'treatments.mode', 'treatments.default', 'treatments.intensity',
    'treatments.max_tail', 'treatments.scenes',
    'delivery.mode', 'delivery.profile', 'delivery.sample_rate', 'delivery.channels',
    'delivery.duration_tolerance', 'delivery.start_tolerance', 'delivery.target_lufs',
    'delivery.lufs_tolerance', 'delivery.true_peak_db', 'delivery.placement_samples',
    'delivery.check_original_streams',
  ];
  for (const key of keys) {
    const field = FIELD_BY_KEY[key];
    assert.ok(field, `${key} has no settings field`);
    assert.ok(field.l, `${key} has no label`);
  }
  // The two that change what is rendered or what is blocked have to explain it.
  assert.match(FIELD_BY_KEY['treatments.mode'].h, /never changes the acting/);
  assert.match(FIELD_BY_KEY['delivery.mode'].h, /withhold the saved version/);
});

test('an unset loudness target is described as measurement, not as a pass', () => {
  assert.match(FIELD_BY_KEY['delivery.target_lufs'].h, /Leave blank to measure only/);
  assert.match(FIELD_BY_KEY['delivery.target_lufs'].h, /never reported as a pass/);
  assert.match(FIELD_BY_KEY['delivery.true_peak_db'].h, /no broadcast or platform compliance/);
});

test('the treatment presets offered are exactly the ones the backend knows', () => {
  assert.deepEqual(FIELD_BY_KEY['treatments.default'].o,
    ['dry', 'room', 'distant', 'phone', 'radio']);
  assert.deepEqual(FIELD_BY_KEY['treatments.mode'].o, ['off', 'on']);
  assert.deepEqual(FIELD_BY_KEY['delivery.mode'].o, ['off', 'measure', 'enforce']);
});

test('a title can choose its own space and export policy', () => {
  const keys = PLAN_FIELDS.map(f => f.k);
  assert.ok(keys.includes('treatments.mode'));
  assert.ok(keys.includes('treatments.default'));
  assert.ok(keys.includes('delivery.mode'));
  // per-episode detail deliberately does not travel to a title plan
  assert.ok(!keys.includes('treatments.scenes'));
  assert.ok(!keys.includes('treatments.lines'));
});

test('the scene player offers the dry line and the treated line separately', () => {
  const kinds = SOURCES.map(s => s.kind);
  assert.ok(kinds.includes('dry'));
  assert.ok(kinds.includes('treated'));
  // both are single files, not views of the scene window, so switching to one
  // must not try to convert a scene position into a window offset
  for (const kind of ['dry', 'treated']) {
    assert.equal(SOURCES.find(s => s.kind === kind).window, undefined);
  }
  // and they sit next to the processed line rather than among the stems
  assert.ok(kinds.indexOf('dry') === kinds.indexOf('line') + 1);
});
