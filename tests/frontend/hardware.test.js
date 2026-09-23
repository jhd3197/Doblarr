import assert from 'node:assert/strict';
import test from 'node:test';

import { cpuReason, hardwareSummary } from '../../web/js/hardware.js';

const GB = 1024 ** 3;

test('a GPU shows its id and live memory use', () => {
  const hw = { torch: { installed: true, cuda: '12.4' },
    devices: [{ id: 'cuda:0', name: 'RTX 4090', usable_by: ['separate', 'diarize', 'transcribe'], total_bytes: 24 * GB, free_bytes: 20 * GB }] };
  const s = hardwareSummary(hw, [{ id: 'cuda:0', total_bytes: 24 * GB, free_bytes: 12 * GB }]);
  assert.equal(s.device, 'cuda:0');
  assert.equal(s.memory, '12 / 24 GB');
  assert.equal(s.percent, 50);
});

test('CPU-only machines say so and why, never a fake GPU', () => {
  assert.equal(hardwareSummary({ torch: { installed: false }, devices: [] }).device, 'CPU only');
  assert.equal(cpuReason({ torch: { installed: false }, devices: [] }), 'PyTorch not installed');
  assert.equal(cpuReason({ torch: { installed: true, cuda: null }, devices: [] }), 'CPU-only PyTorch');
  assert.equal(cpuReason({ torch: { installed: true, cuda: '12.4' }, devices: [] }), 'no CUDA device visible');
  assert.equal(cpuReason(null), 'hardware unknown');
});

test('a card torch cannot use is not presented as the worker device', () => {
  const hw = { torch: { installed: false }, devices: [{ id: 'cuda:0', name: 'RTX', usable_by: [] }] };
  const s = hardwareSummary(hw);
  assert.equal(s.device, 'CPU only');
  assert.equal(s.label, 'GPU found, no PyTorch');
});

test('a card only faster-whisper can use says which stage', () => {
  const hw = { torch: { installed: true, cuda: null },
    devices: [{ id: 'cuda:0', name: 'RTX', usable_by: ['transcribe'] }] };
  assert.equal(hardwareSummary(hw).device, 'cuda:0 (transcribe only)');
});
