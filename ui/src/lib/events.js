import { useSyncExternalStore } from 'react';
import { apiUrl, withKey } from './api.js';
import { queryClient } from './queries.js';

// Live updates from /api/events (SSE). Job events refresh the jobs cache, scan
// events the library counts; log lines feed the Overview log panel. After three
// failures in a row the jobs query falls back to polling every 30 s.
// EventSource can't set headers, so the API key goes on the URL.
const MAX_LOG = 200;
let source = null;
let failures = 0;
let refreshTimer = null;
let logLines = [];
const listeners = new Set();

export const streamDown = () => failures >= 3;

function refreshJobs() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; queryClient.invalidateQueries({ queryKey: ['jobs'] }); }, 100);
}

function pushLog(evt) {
  logLines = [...logLines, `${(evt.at || '').slice(11)} ${evt.level.padEnd(7)} ${evt.logger} | ${evt.message}`].slice(-MAX_LOG);
  listeners.forEach(fn => fn());
}

export function startEventStream() {
  source?.close();
  source = new EventSource(withKey(apiUrl('events')));
  source.onmessage = e => {
    failures = 0;
    let evt;
    try { evt = JSON.parse(e.data); } catch { return; }
    if (evt.topic === 'log') pushLog(evt);
    else if (evt.topic === 'job') refreshJobs();
    else if (evt.topic === 'scan') queryClient.invalidateQueries({ queryKey: ['status'] });
  };
  source.onerror = () => { failures++; };
}
window.addEventListener('doblarr-api-key', startEventStream);  // reconnect with the new key

export function useLogLines() {
  return useSyncExternalStore(fn => { listeners.add(fn); return () => listeners.delete(fn); }, () => logLines);
}
