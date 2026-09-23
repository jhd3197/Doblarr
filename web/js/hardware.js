import { api } from './api.js';

// What the hardware probe (/api/capabilities.hardware) says, in the few words
// the sidebar and overview have room for. Never shows a GPU the probe did not
// find: a CPU-only machine says so, and why.

const GB = 1024 ** 3;

export function gb(bytes) {
  return (bytes / GB).toFixed(bytes >= 10 * GB ? 0 : 1);
}

// Why there is no usable GPU, in a handful of words.
export function cpuReason(hw) {
  if (!hw) return 'hardware unknown';
  if (!hw.torch?.installed) return hw.devices?.length ? 'GPU found, no PyTorch' : 'PyTorch not installed';
  if (!hw.torch.cuda) return 'CPU-only PyTorch';
  return 'no CUDA device visible';
}

// Devices at least one stage can run on (see `usable_by` in hardware.py).
export function usableDevices(hw) {
  return (hw?.devices || []).filter(d => (d.usable_by || []).length);
}

// {device, label, memory, percent} for the sidebar and the overview card.
export function hardwareSummary(hw, memory = null) {
  const devices = usableDevices(hw);
  if (!devices.length) return { device: 'CPU only', label: cpuReason(hw), memory: '', percent: null };
  const first = devices[0];
  const live = (memory || []).find(m => m.id === first.id) || first;
  const total = live.total_bytes, free = live.free_bytes;
  const used = total && free != null ? total - free : null;
  const partial = (first.usable_by || []).length === 1 ? ` (${first.usable_by[0]} only)` : '';
  return {
    device: first.id + partial,
    label: first.name || first.id,
    memory: used != null ? `${gb(used)} / ${gb(total)} GB` : '',
    percent: used != null && total ? Math.round((used / total) * 100) : null,
  };
}

export function renderHardware(hw, memory = null) {
  const s = hardwareSummary(hw, memory);
  const worker = document.getElementById('workerStatus');
  if (worker) worker.textContent = `worker online · ${s.device}`;
  const stat = document.getElementById('statGpu');
  if (stat) stat.textContent = s.percent == null ? '—' : `${s.percent}%`;
  const label = document.getElementById('statGpuLabel');
  if (label) label.textContent = s.memory ? `${s.device} · ${s.memory}` : `${s.device} · ${s.label}`;
  const card = label?.closest('.panel');
  if (card) card.title = [s.label, ...(hw?.notes || [])].join('\n');
}

export async function loadHardware({ refresh = false } = {}) {
  try {
    const hw = await api(refresh ? 'hardware?refresh=1' : 'hardware');
    renderHardware(hw, hw.memory);
    return hw;
  } catch {
    renderHardware(null);
    return null;
  }
}
