// localStorage that never throws: private windows and blocked storage fall
// back to the default.
export function safeGet(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }
// The saved theme; on a first visit, the one the OS asks for.
export function initialTheme() {
  const saved = safeGet('doblarr.theme', '');
  if (saved === 'dark' || saved === 'light') return saved;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
export function safeSet(k, v) { try { localStorage.setItem(k, v); } catch { /* not persisted */ } }
