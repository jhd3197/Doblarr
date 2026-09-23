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

test('the Hardware tab holds the device and memory controls', () => {
  const hardware = TABS.find(t => t.id === 'hardware');
  assert.ok(hardware, 'no Hardware tab');
  const keys = hardware.groups.flatMap(g => g.fields.map(f => f.k).filter(Boolean));
  assert.deepEqual(keys, [
    'compute.device', 'compute.separate_device', 'compute.diarize_device',
    'compute.transcribe_device', 'compute.release_after_stage', 'compute.log_memory',
    'transcribe.compute_type', 'transcribe.keep_models_loaded', 'separate.chunk_seconds',
  ]);
  const info = hardware.groups[0].fields[0];
  assert.equal(info.t, 'info');
  assert.equal(info.k, undefined, 'a read-only block is never saved');
  assert.ok(!('hardware' in FIELD_BY_KEY));
  // moved, not duplicated: the Transcript tab no longer offers them
  const speech = TABS.find(t => t.id === 'speech').groups.flatMap(g => g.fields.map(f => f.k));
  assert.ok(!speech.includes('transcribe.device'));
  assert.ok(!speech.includes('transcribe.compute_type'));
  assert.ok(!speech.includes('transcribe.keep_models_loaded'));
});

test('device choices come from the probe, with a fallback and the saved value kept', async () => {
  const { applyDeviceOptions, deviceChoices } = await import('../../web/js/settings-model.js');
  assert.deepEqual(FIELD_BY_KEY['compute.device'].o, ['auto', 'cpu', 'cuda', 'mps']);
  assert.deepEqual(deviceChoices({ devices: [] }), ['auto', 'cpu']);
  const hw = { devices: [
    { id: 'cuda:0', usable_by: ['separate', 'diarize', 'transcribe'] },
    { id: 'cuda:1', usable_by: ['separate', 'diarize', 'transcribe'] },
    { id: 'cuda:2', usable_by: [] },
  ] };
  applyDeviceOptions(hw, key => (key === 'compute.separate_device' ? 'cuda:5' : undefined));
  assert.deepEqual(FIELD_BY_KEY['compute.device'].o, ['auto', 'cpu', 'cuda', 'cuda:0', 'cuda:1']);
  assert.deepEqual(FIELD_BY_KEY['compute.diarize_device'].o,
    ['inherit', 'auto', 'cpu', 'cuda', 'cuda:0', 'cuda:1']);
  assert.ok(FIELD_BY_KEY['compute.separate_device'].o.includes('cuda:5'));
  applyDeviceOptions(null);  // probe unavailable: choices stay as they were
  assert.ok(FIELD_BY_KEY['compute.device'].o.includes('cuda:0'));
});

test('steady pacing is a visible timing choice a title can make', () => {
  assert.deepEqual(FIELD_BY_KEY['timing.pacing'].o, ['speaker', 'off']);
  assert.match(FIELD_BY_KEY['timing.pacing'].h, /as earlier releases did/);
  for (const key of ['timing.pace_tolerance', 'timing.pace_local_range',
    'timing.pace_max_speedup', 'timing.pace_scene_gap']) assert.ok(FIELD_BY_KEY[key]?.h, key);
  assert.ok(PLAN_FIELDS.map(f => f.k).includes('timing.pacing'));
});
