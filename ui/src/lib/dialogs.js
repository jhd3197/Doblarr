import { useSyncExternalStore } from 'react';

// In-app replacements for window.confirm / window.prompt. Calls queue up and
// the shell shows one at a time; each returns a promise settled by the user.
let queue = [];
let serial = 0;
const listeners = new Set();
const changed = () => listeners.forEach(fn => fn());

function ask(dialog) {
  return new Promise(resolve => { queue = [...queue, { ...dialog, id: ++serial, resolve }]; changed(); });
}

// Resolves true on confirm, false on cancel / Escape / backdrop.
export const confirmDialog = ({ title, message = '', confirmLabel = 'OK', danger = false }) =>
  ask({ kind: 'confirm', title, message, confirmLabel, danger });

// Resolves the entered text, or null on cancel / Escape / backdrop.
export const promptDialog = ({ title, message = '', label = '', defaultValue = '' }) =>
  ask({ kind: 'prompt', title, message, label, defaultValue, confirmLabel: 'OK' });

export const currentDialog = () => queue[0] || null;

// Settles the open dialog; `value` is the answer, or undefined to cancel.
export function settleDialog(value) {
  const [open, ...rest] = queue;
  if (!open) return;
  queue = rest;
  open.resolve(open.kind === 'confirm' ? value === true : (value ?? null));
  changed();
}

export function useDialog() {
  return useSyncExternalStore(fn => { listeners.add(fn); return () => listeners.delete(fn); }, currentDialog);
}
