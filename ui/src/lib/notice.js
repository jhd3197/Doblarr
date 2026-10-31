import { useSyncExternalStore } from 'react';

// One app-wide line for a failed action, shown by the shell instead of a
// blocking window.alert. A newer message replaces the last one.
const SHOWN_MS = 8000;
let current = null;
let timer = null;
const listeners = new Set();
const changed = () => listeners.forEach(fn => fn());

export function notify(message) {
  clearTimeout(timer);
  current = { message: String(message), at: Date.now() };
  timer = setTimeout(dismiss, SHOWN_MS);
  changed();
}

export function dismiss() {
  clearTimeout(timer);
  current = null;
  changed();
}

export function useNotice() {
  return useSyncExternalStore(fn => { listeners.add(fn); return () => listeners.delete(fn); }, () => current);
}
